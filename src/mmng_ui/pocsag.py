from __future__ import annotations

import asyncio
import csv
import io
import json as json_lib
import os
import shlex
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from subprocess import PIPE
from typing import ClassVar

import click
from rich.emoji import EMOJI
from rich.text import Text
from rich.text import Text as RichText
from textual import events, work
from textual.actions import SkipAction
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Container, Horizontal, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Digits,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RadioSet,
    RichLog,
    Rule,
    Sparkline,
    TabbedContent,
    TabPane,
)

from mmng_ui._version import __version__
from mmng_ui.capcode_db import CapcodeDB
from mmng_ui.reader import ParseLine
from mmng_ui.sdr import RTLFMReceiver, SDRConfig, SdrError
from mmng_ui.sdr_profiles import (
    FrequencyProfile,
    ProfileCatalog,
    ProfileStoreError,
    default_profile_path,
    load_profiles,
    save_profiles,
)


def parse_ports(port_str: str) -> list[PortConfig]:
    """Parse a comma-separated port string into PortConfig objects.

    Supports port=Name or port:Name syntax for custom tab names.
    """
    if not port_str:
        raise ValueError('Port string cannot be empty')
    configs = []
    for part in port_str.split(','):
        stripped = part.strip()
        if not stripped:
            raise ValueError(f'Invalid port string: {port_str!r}')
        if '=' in stripped:
            port_part, name_part = stripped.split('=', 1)
            name = name_part.strip()
            if not name:
                raise ValueError(f'Empty tab name in port string: {stripped!r}')
            try:
                configs.append(PortConfig(port=int(port_part.strip()), name=name))
            except ValueError:
                raise ValueError(f'Invalid port value: {port_part.strip()!r}')
        elif ':' in stripped:
            port_part, name_part = stripped.split(':', 1)
            name = name_part.strip()
            if not name:
                raise ValueError(f'Empty tab name in port string: {stripped!r}')
            try:
                configs.append(PortConfig(port=int(port_part.strip()), name=name))
            except ValueError:
                raise ValueError(f'Invalid port value: {port_part.strip()!r}')
        else:
            try:
                configs.append(PortConfig(port=int(stripped)))
            except ValueError:
                raise ValueError(f'Invalid port value: {stripped!r}')
    return configs


@dataclass
class OutputMessage(Message, bubble=False):
    """Custom message class to handle subprocess output."""

    output: str


@dataclass
class Status:
    """The status pane"""

    receiver: str
    ip_address: str
    json_mode: bool
    charset: str

    def __repr__(self):
        return f'Receiver: {self.receiver}\nIP address: {self.ip_address}'


@dataclass
class Executable:
    """An info class for the executables to run."""

    command: str
    resolved_path: str
    version: str | None


@dataclass
class PortConfig:
    """A port with an optional custom tab name."""

    port: int
    name: str | None = None


class UDPHandler(asyncio.DatagramProtocol):
    """Handle UDP traffic"""

    def __init__(self, app, loop, port):
        self.app = app
        self.loop = loop
        self.port = port
        self.status = self.app.query_one(f'#status-{port}')
        self.last_activity_time = 0

    def connection_made(self, transport):
        self.transport = transport
        self.status.receiver = 'ready'

    def connection_lost(self, exc):
        self.status.receiver = 'Closed'

    def datagram_received(self, data, addr):
        sdr_session = getattr(self.app, 'sdr_session', None)
        if sdr_session is not None and sdr_session.state in ('starting', 'running', 'stopping', 'cleanup_failed'):
            return
        self.last_activity_time = self.loop.time()
        self.status.ip_address = addr[0]
        if self.app.app.sox_rate:
            stdin = self.app.sox_process.stdin
        else:
            stdin = self.app.process.stdin
        stdin.write(data)
        stdin.drain()

    async def idle_task(self):
        """This updates the things in the status pane."""
        while True:
            if self.loop.time() - self.last_activity_time > 5:
                self.status.receiver = '[wheat4]idle[/]'
            await asyncio.sleep(1)


class StatusWidget(Widget):
    """The status pane."""

    receiver = reactive('[dark_red]Not connected[/]')
    ip_address = reactive('[wheat4]None[/]')
    json_mode = reactive('[wheat4]Unknown[/]')
    charset = reactive('[wheat4]Unknown[/]')

    def render(self) -> str:
        return f'Receiver: {self.receiver}\nIP address: {self.ip_address}\nJSON mode: {self.json_mode}\nCharset: {self.charset}'


class HelpScreen(ModalScreen):
    """Help screen modal."""

    BINDINGS = [('escape,space,q,question_mark', 'app.pop_screen', 'Close')]

    def compose(self) -> ComposeResult:
        text = """
# mmng-ui

## Introduction

This is a TUI utility to decode and see POCSAG messages.

mmng-ui will listen on chosen UDP ports for raw streams from software like SDR++, use
[multimon-ng](https://github.com/EliasOenal/multimon-ng) to decode it, and show you POCSAG messages in a wonderful
text interface.

## Usage

Alpha POCSAG messages will display in the top pane.  The bottom pane will show the raw output from `multimon-ng`,
as well as any errors or issues with decoding.

The status panel shows any incoming connections.  Receiver will transition between the following states:

| Receiver state | Description                                     |
|----------------|-------------------------------------------------|
| idle           | No UDP traffic yet seen, or seen in 5 seconds   |
| receiving      | Actively receiving a decode from `multimon-ng`  |
| waiting        | Traffic is coming in, but nothing to be decoded |

Just below the status panel is a sparkline -- this updates on each decode, and reflects character length of said
decode.

Underneath the log window in another sparkline, and this shows messages per second, for the last minute.

The footer shows available keyboard choices to quit the app, show a help screen, and clear all logging panes.

The mouse will also work!

## JSON mode

`mmng-ui` will attempt to auto-detect the output format from `multimon-ng`, and if it looks like JSON, it'll use it.

JSON output was merged into `multimon-ng` [version 1.4.0](https://github.com/EliasOenal/multimon-ng/releases/tag/1.4.0).

[//]: # (README.md ends here)"""
        yield Markdown(text, id='help')


class AboutScreen(ModalScreen):
    """About/info screen modal."""

    BINDINGS = [('escape,space,q', 'app.pop_screen', 'Close')]

    def compose(self) -> ComposeResult:
        mmng_info = f"""
## multimon-ng

```
Path: {self.app.mmng.resolved_path}
Version: {self.app.mmng.version}
```
        """
        if self.app.sox_rate:
            sox_info = f"""
## sox

```
Path: {self.app.sox.resolved_path}
Version: {self.app.sox.version}
```
        """
        with Container(id='about'):
            yield Markdown('# This is mmng-ui!')
            with Center():
                yield Label('Version', classes='version')
            with Center():
                yield Digits(__version__, classes='version')
            yield Rule(line_style='double')
            with Container(id='app-versions'):
                yield Markdown(mmng_info)
                if self.app.sox_rate:
                    yield Markdown(sox_info)


class MsgsPerSecond(Sparkline):
    """Calculate/update the messages per second sparkline."""

    def __init__(self, samples=None, message_count_ref=None, **kwargs):
        super().__init__(**kwargs)
        self.samples = samples if samples is not None else [0] * 60
        self.message_count_ref = message_count_ref

    def on_mount(self) -> None:
        self.update_timer = self.set_interval(1, self.update_graph)
        self.data = self.samples
        self.styles.height = '1'

    def update_graph(self) -> None:
        count = len(self.message_count_ref)
        self.data = self.data[-59:] + [count]
        self.message_count_ref.clear()


class FeedTabbedContent(TabbedContent):
    """TabbedContent with height: 1fr to override TabbedContent's DEFAULT_CSS height: auto."""

    DEFAULT_CSS = """
    FeedTabbedContent {
        height: 1fr;
    }
    """

    def on_mount(self) -> None:
        switcher = self.get_child_by_type(ContentSwitcher)
        switcher.styles.height = '1fr'


class FeedTabPane(TabPane):
    """TabPane with height: 100% to override TabPane's DEFAULT_CSS height: auto."""

    DEFAULT_CSS = """
    FeedTabPane {
        height: 100%;
    }
    """


class RenameTabScreen(ModalScreen):
    """Modal screen for renaming the active tab."""

    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, current_title: str) -> None:
        super().__init__()
        self.current_title = current_title

    def compose(self) -> ComposeResult:
        with Container():
            yield Label('Rename tab:')
            yield Input(value=self.current_title, placeholder='Enter new tab name')

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if name:
            pane = self.app._rename_pane
            pane._title = name
            tab = pane.parent.parent.get_tab(pane.id)
            tab.label = name
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()

    def on_mount(self) -> None:
        self.query_one(Input).focus()


def _extract_table_rows(table: DataTable) -> list[dict[str, str]]:
    """Extract rows from a DataTable as list of dicts with plain text values."""
    rows = []
    for row_key in table.rows:
        raw_time, raw_address, raw_message = table.get_row(row_key)
        if isinstance(raw_address, RichText):
            raw_address = raw_address.plain
        if isinstance(raw_message, RichText):
            raw_message = raw_message.plain
        rows.append({
            'time': str(raw_time),
            'address': str(raw_address),
            'message': str(raw_message),
        })
    return rows


def _format_as_csv(rows: list[dict[str, str]]) -> str:
    """Format rows as CSV with header row."""
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Time', 'Address', 'Message'])
    for row in rows:
        writer.writerow([row.get('time', ''), row.get('address', ''), row.get('message', '')])
    return output.getvalue()


def _format_as_markdown(rows: list[dict[str, str]]) -> str:
    """Format rows as a Markdown pipe table."""
    rows_formatted = [
        f"| {row.get('time', '')} | {row.get('address', '')} | {row.get('message', '')} |"
        for row in rows
    ]
    return '\n'.join(['| Time | Address | Message |', '| --- | --- | --- |', *rows_formatted]) + '\n'


def _format_as_json(rows: list[dict[str, str]]) -> str:
    """Format rows as a JSON array of objects with capitalized keys."""
    items = [
        {
            'Time': row.get('time', ''),
            'Address': row.get('address', ''),
            'Message': row.get('message', ''),
        }
        for row in rows
    ]
    return json_lib.dumps(items, indent=2)


FORMAT_MAP = {
    'CSV': _format_as_csv,
    'Markdown': _format_as_markdown,
    'JSON': _format_as_json,
}


class SaveScreen(ModalScreen):
    """Modal screen for saving the current tab's messages to a file."""

    BINDINGS = [('escape', 'cancel', 'Cancel')]

    def __init__(self, rows: list[dict[str, str]], port: int) -> None:
        super().__init__()
        self.rows = rows
        self.port = port
        self.selected_format = 'CSV'

    def compose(self) -> ComposeResult:
        timestamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        default_filename = f'mmng_port{self.port}_{timestamp}.csv'
        with Container(id='save-dialog'):
            yield Label('Save messages to file:')
            yield Input(value=default_filename, placeholder='Enter filename', id='save-filename')
            yield Label('Format:')
            yield RadioSet('CSV', 'Markdown', 'JSON', id='save-format')
            with Horizontal(id='save-buttons'):
                yield Button('Save', variant='primary', id='save-submit')
                yield Button('Cancel', id='save-cancel')

    def on_mount(self) -> None:
        radio_set = self.query_one('#save-format', RadioSet)
        buttons = radio_set.query('RadioButton')
        if buttons:
            buttons.first().value = True

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        self.selected_format = str(event.pressed.label)
        filename_input = self.query_one('#save-filename', Input)
        current = filename_input.value.strip()
        base = current.rsplit('.', 1)[0] if '.' in current else current
        ext = {'CSV': 'csv', 'Markdown': 'md', 'JSON': 'json'}[self.selected_format]
        filename_input.value = f'{base}.{ext}'

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == 'save-submit':
            self._do_save()
        elif event.button.id == 'save-cancel':
            self.app.pop_screen()

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._do_save()

    def _do_save(self) -> None:
        filename_input = self.query_one('#save-filename', Input)
        filename = filename_input.value.strip() or f'mmng_port{self.port}.csv'
        formatter = FORMAT_MAP.get(self.selected_format, _format_as_csv)
        content = formatter(self.rows)
        string_io = io.StringIO(content)
        self.app.deliver_text(string_io, save_filename=filename)
        self.app.notify(f'Saved as {filename}', title='Export')
        self.app.pop_screen()

    def action_cancel(self) -> None:
        self.app.pop_screen()


class ProfileEditorScreen(ModalScreen):
    """Edit draft settings; persistence belongs to the app's guarded handlers."""

    BINDINGS: ClassVar = [('escape', 'cancel', 'Cancel')]

    def __init__(self, profile: FrequencyProfile | None = None) -> None:
        super().__init__()
        self.profile = profile

    def compose(self) -> ComposeResult:
        config = self.profile.config if self.profile else None
        with Container(id='profile-editor'):
            yield Label('Edit SDR profile' if self.profile else 'New SDR profile')
            with VerticalScroll(id='profile-fields'):
                for field, label, value in (
                    ('name', 'Profile name', self.profile.name if self.profile else ''),
                    ('frequency', 'Frequency (Hz)', str(config.frequency_hz) if config else ''),
                    ('device', 'RTL-SDR device index (no discovery)', str(config.device_index) if config else '0'),
                    ('gain', 'Gain (dB, 0-50)', str(config.gain_db) if config else ''),
                    ('ppm', 'PPM correction (-1000 to 1000)', str(config.ppm_correction) if config else '0'),
                ):
                    yield Label(label)
                    yield Input(value=value, id=f'profile-{field}')
            yield Label('', id='profile-error', markup=False)
            with Horizontal(classes='profile-buttons'):
                yield Button('Save', id='profile-save', variant='primary')
                yield Button('Cancel', id='profile-cancel')

    def on_mount(self) -> None:
        self.query_one('#profile-error', Label).styles.width = '100%'
        self.query_one('#profile-name', Input).focus()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == 'profile-cancel':
            self.action_cancel()
        elif event.button.id == 'profile-save':
            try:
                values = {
                    field: self.query_one(f'#profile-{field}', Input).value
                    for field in ('name', 'frequency', 'device', 'gain', 'ppm')
                }
                profile = FrequencyProfile(
                    values['name'],
                    SDRConfig(
                        int(values['frequency']), float(values['gain']), int(values['device']), int(values['ppm'])
                    ),
                )
                if self.app.save_profile(profile, self.profile.name if self.profile else None):
                    self.dismiss()
                else:
                    self.query_one('#profile-error', Label).update(self.app.profile_notice)
            except (ValueError, TypeError) as exc:
                self.query_one('#profile-error', Label).update(str(exc))

    def action_cancel(self) -> None:
        self.dismiss()


class ProfileDeleteScreen(ModalScreen):
    BINDINGS: ClassVar = [('escape', 'cancel', 'Cancel')]

    def __init__(self, name: str) -> None:
        super().__init__()
        self.profile_name = name

    def compose(self) -> ComposeResult:
        with Container(id='profile-delete-dialog'):
            yield Label(f'Delete profile {self.profile_name}?', markup=False)
            with Horizontal(classes='profile-buttons'):
                yield Button('Delete', id='profile-confirm-delete', variant='error')
                yield Button('Cancel', id='profile-cancel')

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        if event.button.id == 'profile-cancel' or (
            event.button.id == 'profile-confirm-delete' and self.app.delete_profile(self.profile_name)
        ):
            self.dismiss()

    def action_cancel(self) -> None:
        self.dismiss()


class ProfilesScreen(ModalScreen):
    BINDINGS: ClassVar = [('escape', 'cancel', 'Close')]

    def compose(self) -> ComposeResult:
        with Container(id='profiles-dialog'):
            yield Label('SDR profiles (selection never starts reception)')
            yield Label('', id='profile-effective', markup=False)
            yield DataTable(id='profile-list', cursor_type='row')
            yield Label('', id='profile-notice', markup=False)
            with Horizontal(classes='profile-buttons'):
                yield Button('New', id='profile-new')
                yield Button('Edit', id='profile-edit')
                yield Button('Select', id='profile-select')
                yield Button('Delete', id='profile-delete')
                yield Button('Close', id='profile-close')

    def on_mount(self) -> None:
        self.query_one(DataTable).add_columns('Name', 'Hz', 'Device', 'Gain dB', 'PPM')
        self.refresh_profiles()
        self.query_one(DataTable).focus()

    def refresh_profiles(self) -> None:
        table = self.query_one(DataTable)
        row = table.cursor_row
        table.clear()
        for profile in self.app.profile_catalog.profiles:
            config = profile.config
            table.add_row(
                Text(profile.name),
                config.frequency_hz,
                config.device_index,
                config.gain_db,
                config.ppm_correction,
                key=profile.name,
            )
        if table.row_count:
            table.move_cursor(row=min(row, table.row_count - 1))
        self.query_one('#profile-effective', Label).update(
            f'Stored selection: {self.app.profile_catalog.selected_name or "none"}\n'
            f'Effective configuration: {self.app.profile_source}'
        )
        self.query_one('#profile-notice', Label).update(self.app.profile_notice)
        editable = self.app.profiles_editable
        self.query_one('#profile-new', Button).disabled = not editable
        for action in ('edit', 'select', 'delete'):
            self.query_one(f'#profile-{action}', Button).disabled = not editable or not table.row_count

    def on_button_pressed(self, event: Button.Pressed) -> None:
        event.stop()
        action = event.button.id
        if action == 'profile-close':
            self.dismiss()
            return
        if not self.app.profiles_editable:
            return
        if action == 'profile-new':
            self.app.push_screen(ProfileEditorScreen())
            return
        table = self.query_one(DataTable)
        if not table.row_count:
            return
        profile = self.app.profile_catalog.profiles[table.cursor_row]
        if action == 'profile-edit':
            self.app.push_screen(ProfileEditorScreen(profile))
        elif action == 'profile-select':
            self.app.select_profile(profile.name)
        elif action == 'profile-delete':
            self.app.push_screen(ProfileDeleteScreen(profile.name))

    def action_cancel(self) -> None:
        self.dismiss()


class FeedWidget(Widget):
    """A widget representing a single POCSAG feed on one UDP port."""

    def __init__(self, port: int, capcode_db: CapcodeDB | None = None, **kwargs):
        super().__init__(**kwargs)
        self.port = port
        self.capcode_db = capcode_db
        self.process = None
        self.sox_process = None
        self.sdr_session = None
        self._sdr_start_worker = None
        self._sdr_stop_worker = None
        self._sdr_unmounting = False
        self._sdr_replacement_request = None
        self.message_count = []

    DEFAULT_CSS = """
FeedWidget {
    height: 100%;
    layout: vertical;
}
"""

    def compose(self) -> ComposeResult:
        with Widget(classes='feed-grid'):
            yield DataTable(id=f'messages-{self.port}', classes='feed-messages')
            yield RichLog(id=f'log-{self.port}', highlight=True, markup=True, classes='feed-log')
            with Widget(classes='feed-status-container'):
                yield StatusWidget(id=f'status-{self.port}', classes='feed-status')
                yield Sparkline([], id=f'spark-{self.port}')
        yield MsgsPerSecond(id=f'msgs-per-second-{self.port}', message_count_ref=self.message_count, classes='feed-mps')
        with Horizontal(classes='sdr-controls'):
            yield Button(
                'Start SDR',
                id=f'sdr-start-{self.port}',
                disabled=getattr(self.app, 'sdr_config', None) is None,
            )
            yield Button('Stop SDR', id=f'sdr-stop-{self.port}', disabled=True)
            yield Label('RTL-SDR process: stopped', id=f'sdr-state-{self.port}')

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == f'sdr-start-{self.port}' and (
            self._sdr_start_worker is None or self._sdr_start_worker.is_finished
        ):
            self._sdr_start_worker = self.run_worker(
                self._run_sdr_start(),
                group=f'sdr-start-{self.port}',
                name=f'sdr-start-{self.port}',
                exclusive=False,
                exit_on_error=False,
            )
        elif event.button.id == f'sdr-stop-{self.port}' and (
            self._sdr_stop_worker is None or self._sdr_stop_worker.is_finished
        ):
            self._sdr_stop_worker = self.run_worker(
                self._run_sdr_stop(),
                group=f'sdr-stop-{self.port}',
                name=f'sdr-stop-{self.port}',
                exclusive=False,
                exit_on_error=False,
            )

    async def _run_sdr_start(self) -> None:
        try:
            await self.start_sdr()
        except Exception as exc:  # noqa: BLE001 - retain and display unexpected worker failures
            if not self._sdr_unmounting:
                state = (
                    self.sdr_session.state
                    if self.sdr_session and self.sdr_session.state == 'cleanup_failed'
                    else 'error'
                )
                self._show_sdr_state(state, str(exc))
        finally:
            self._sdr_start_worker = None
            getattr(self.app, 'refresh_profile_views', lambda: None)()

    async def _run_sdr_stop(self) -> None:
        try:
            await self.stop_sdr()
        except Exception as exc:  # noqa: BLE001 - retain and display unexpected worker failures
            if not self._sdr_unmounting:
                self._show_sdr_state('cleanup_failed', str(exc))
        finally:
            self._sdr_stop_worker = None
            getattr(self.app, 'refresh_profile_views', lambda: None)()

    async def start_sdr(self) -> None:
        """Start configured RTL-SDR acquisition only after explicit TUI input."""
        if self.app.is_web or self._sdr_unmounting:
            return
        config = getattr(self.app, 'sdr_config', None)
        if config is None:
            return
        if self.app.sox_rate and str(self.app.sox_rate) != '22050':
            self._show_sdr_state('error', 'SoX input rate must be 22050 for RTL-SDR')
            return
        if self.app.sox_rate:
            sink = self.sox_process.stdin if self.sox_process is not None else None
        else:
            sink = self.process.stdin if self.process is not None else None
        if sink is None:
            self._show_sdr_state('error', 'multimon-ng input is unavailable')
            return
        try:
            if self.sdr_session is not None and self.sdr_session.config != config:
                if self.sdr_session.state not in ('stopped', 'error'):
                    return
                request = object()
                self._sdr_replacement_request = request
                try:
                    await self.sdr_session.stop()
                    # Stop/unmount intent must survive the old session's cleanup await.
                    if self._sdr_replacement_request is not request or self._sdr_unmounting:
                        return
                    if self.sdr_session.state not in ('stopped', 'error'):
                        return
                    self.sdr_session = None
                finally:
                    if self._sdr_replacement_request is request:
                        self._sdr_replacement_request = None
            if self.sdr_session is None:
                self.sdr_session = RTLFMReceiver(config, sink, on_state=self._show_sdr_state)
            await self.sdr_session.start()
        except SdrError as exc:
            state = 'cleanup_failed' if self.sdr_session and self.sdr_session.state == 'cleanup_failed' else 'error'
            self._show_sdr_state(state, str(exc))

    async def stop_sdr(self) -> None:
        self._sdr_replacement_request = None
        if self.sdr_session is not None:
            await self.sdr_session.stop()

    async def on_unmount(self) -> None:
        self._sdr_unmounting = True
        self._sdr_replacement_request = None
        if self.sdr_session is not None:
            try:
                await self.sdr_session.stop()
            except Exception:  # noqa: BLE001 - shutdown must retain unresolved ownership
                # The receiver retains ownership and exposes cleanup_failed/error.
                return

    def _show_sdr_state(self, state: str, error: str | None) -> None:
        if self._sdr_unmounting or not self.is_mounted:
            return
        label = self.query_one(f'#sdr-state-{self.port}', Label)
        active = state in ('starting', 'running', 'stopping', 'cleanup_failed')
        text = f'RTL-SDR process: {state}' + (' (UDP paused)' if active else '')
        label.update(text + (f' - {error}' if error else ''))
        start = self.query_one(f'#sdr-start-{self.port}', Button)
        stop = self.query_one(f'#sdr-stop-{self.port}', Button)
        start.disabled = active or getattr(self.app, 'sdr_config', None) is None
        stop.disabled = not active
        getattr(self.app, 'refresh_profile_views', lambda: None)()

    async def on_mount(self) -> None:
        """Setup the initial UI components."""
        table = self.query_one(f'#messages-{self.port}')
        log = self.query_one(f'#log-{self.port}')
        status = self.query_one(f'#status-{self.port}')
        spark = self.query_one(f'#spark-{self.port}')

        # Inline styles override DEFAULT_CSS for proper grid stretching
        spark.styles.height = '100%'

        table.add_column('Time', key='time')
        table.add_column('Address', key='address')
        table.add_column('Message', key='message')
        table.cursor_type = 'none'
        table.columns['message'].auto_width = False
        table.border_title = 'Messages'
        log.border_title = 'Log window'
        status.border_title = 'Status'

        self.parse_line = ParseLine()

        status.charset = self.app.charset

    def on_resize(self, event: events.Resize) -> None:
        table = self.query_one(f'#messages-{self.port}')
        if not table.columns or not table.visible:
            return
        time_w = table.columns['time'].get_render_width(table)
        addr_w = table.columns['address'].get_render_width(table)
        scroll_pad = table.styles.scrollbar_size_vertical if table.show_vertical_scrollbar else 0
        available = table.size.width - time_w - addr_w - (2 * table.cell_padding) - scroll_pad
        table.columns['message'].width = max(available, 20)

    def start_streaming(self):
        """Build multimon-ng args and start the subprocess/UDP pipeline."""
        if getattr(self.app, '_streaming_disabled', False):
            return
        log = self.query_one(f'#log-{self.port}')
        log.write(f'multimon-ng version: {self.app.mmng.version}')
        log.write(f'JSON capable: {self.app.json_capable}')
        mmng_args = f'-a POCSAG512 -a POCSAG1200 -a POCSAG2400 -a FLEX -a FLEX_NEXT -f alpha -t raw -u -q --timestamp -p {"--json" if self.app.json_capable else ""} -C {self.app.charset} -'
        self.stream_subprocess(self.app.mmng_binary, mmng_args)

    @work(exclusive=True)
    async def stream_subprocess(self, command, args):
        """Stream output from a subprocess and post it using post_message."""
        if self.app.sox_rate:
            sox_args = f'-t raw -esigned-integer -b16 -r{self.app.sox_rate} - -t raw -esigned-integer -b16 -r22050 -'
            sox_read, sox_write = os.pipe()
            self.sox_process = await asyncio.create_subprocess_exec(
                self.app.sox_binary, *shlex.split(sox_args), stdin=PIPE, stdout=sox_write, stderr=PIPE
            )
            os.close(sox_write)

            self.process = await asyncio.create_subprocess_exec(
                command, *shlex.split(args), stdin=sox_read, stdout=PIPE, stderr=PIPE
            )
            os.close(sox_read)
        else:
            self.process = await asyncio.create_subprocess_exec(
                command, *shlex.split(args), stdin=PIPE, stdout=PIPE, stderr=PIPE
            )

        network_loop = asyncio.get_running_loop()
        transport, protocol = await network_loop.create_datagram_endpoint(
            lambda: UDPHandler(self, network_loop, self.port), local_addr=('::', self.port)
        )
        network_loop.create_task(protocol.idle_task())

        # Stream stdout asynchronously
        async for line in self.read_process_output(self.process.stdout):
            self.post_message(OutputMessage(line))
            self.set_timer(
                1, lambda: setattr(self.query_one(f'#status-{self.port}'), 'receiver', '[dark_green]waiting[/]')
            )
            self.query_one(f'#spark-{self.port}').data = self.query_one(f'#spark-{self.port}').data[-9:] + [len(line)]
            self.message_count.append(1)

        # Handle any stderr errors
        async for error in self.read_process_output(self.process.stderr):
            self.post_message(OutputMessage(f'[red]Error: {error}'))
        async for error in self.read_process_output(self.sox_process.stderr):
            self.post_message(OutputMessage(f'[red]Error: {error}'))

    async def read_process_output(self, output):
        """Read the output of a subprocess line by line."""
        status = self.query_one(f'#status-{self.port}')
        while True:
            status.receiver = '[blink bold bright_green]receiving[/]'
            line = await output.readline()
            if not line:
                break
            yield line.decode().strip()

    async def on_output_message(self, message: OutputMessage):
        """Handle OutputMessage to update UI components."""
        log = self.query_one(f'#log-{self.port}')
        table = self.query_one(f'#messages-{self.port}')
        status = self.query_one(f'#status-{self.port}')

        # Process the output as it becomes available
        log.write(f'[bold magenta]multimon-ng: {message.output}')

        result, json_detected = self.parse_line.parse(message.output)

        status.json_mode = json_detected

        if result.trim_message:
            raw_address = str(result.address)
            entry = self.capcode_db.lookup(raw_address) if self.capcode_db else None
            if entry:
                emoji = EMOJI.get(entry.icon, '') if entry.icon else ''
                prefix = f'{emoji} ' if emoji else ''
                addr_renderable = Text(f'{prefix}{entry.alias}', style=entry.color or '', justify='right')
                addr_renderable.append(f' ({raw_address})', style='dim')
            else:
                addr_renderable = Text(raw_address, justify='right')
            table.add_row(
                str(result.current_time.strftime('%H:%M:%S')),
                addr_renderable,
                Text(result.trim_message, overflow='fold'),
                height=None,
            )
        else:
            log.write('WARNING: No valid message decoded from multimon-ng')

        try:
            table.action_scroll_bottom()
        except SkipAction:
            pass

    def clear(self):
        self.query_one(f'#messages-{self.port}').clear()
        self.query_one(f'#log-{self.port}').clear()


class MainScreen(Screen):
    def compose(self):
        yield Header()
        with FeedTabbedContent():
            for cfg in self.app.port_configs:
                title = cfg.name or f'Port {cfg.port}'
                with FeedTabPane(title, id=f'tab-{cfg.port}'):
                    yield FeedWidget(port=cfg.port, capcode_db=self.app.capcode_db)
        yield Footer()

    async def on_mount(self) -> None:
        """Setup the initial components."""
        self.title = 'multimon-ng decoder'
        version, json_capable = await self._detect_mmng_version()
        self.app.mmng.version = version
        self.app.json_capable = json_capable
        for feed in self.query(FeedWidget):
            feed.start_streaming()

    async def _detect_mmng_version(self) -> tuple[str, bool]:
        """Returns (version_string, json_capable)."""
        try:
            mmng_help_process = await asyncio.create_subprocess_exec(self.app.mmng_binary, '-h', stderr=PIPE)
            mmng_help = await mmng_help_process.stderr.read()
            await mmng_help_process.wait()
            mmng_text = mmng_help.decode()
            json_capable = '--json' in mmng_text
            version = mmng_text.splitlines()[0].split()[1]
            return version, json_capable
        except OSError:
            return '0.0.0', False


class Pocsag(App):
    def __init__(
        self,
        mmng_binary: str,
        port_configs: list[PortConfig],
        charset: str,
        sox_binary: str | None,
        sox_rate: int | None,
        capcode_db: CapcodeDB | None = None,
        sdr_config: SDRConfig | None = None,
    ) -> None:
        self.mmng_binary = mmng_binary
        self.sox_binary = sox_binary
        self.port_configs = port_configs
        self.charset = charset
        self.sox_rate = sox_rate
        self.capcode_db = capcode_db
        self.sdr_config = sdr_config
        self.profile_catalog = ProfileCatalog()
        self.profile_source = 'CLI' if sdr_config is not None else 'none'
        self.profile_notice = ''
        self._profile_path = None
        self._profile_error = False
        self.mmng = Executable(command=mmng_binary, resolved_path=shutil.which(mmng_binary), version=None)
        if self.sox_binary and self.sox_rate:
            self.sox = Executable(command=sox_binary, resolved_path=shutil.which(sox_binary) or None, version=None)
        self.json_capable = False
        super().__init__()

    @property
    def ports(self) -> list[int]:
        return [pc.port for pc in self.port_configs]

    CSS_PATH = 'pocsag.tcss'

    SCREENS = {'help': HelpScreen}

    BINDINGS = [
        Binding(key='q', action='quit', description='Quit the app'),
        Binding(
            key='question_mark',
            action="app.push_screen('help')",
            description='Show help screen',
            key_display='?',
        ),
        Binding(key='c', action='clear_screen', description='Clear all panes'),
        Binding(
            key='a',
            action='about',
            description='About/info',
            key_display='a',
        ),
        Binding(key='r', action='rename_tab', description='Rename tab'),
        Binding(key='s', action='save_as', description='Save tab to file'),
        Binding(key='p', action='sdr_profiles', description='SDR profiles'),
    ]

    def on_mount(self):
        # is_web is reliable only after Textual has installed the active driver.
        if self.profiles_local:
            self._profile_path = default_profile_path()
            try:
                self.profile_catalog = load_profiles(self._profile_path)
                if self.sdr_config is None:
                    self._apply_profile_selection()
            except ProfileStoreError as exc:
                self._profile_error = True
                self.profile_notice = str(exc)
        else:
            self.sdr_config = None
            self.profile_source = 'unavailable in web mode'
        self.push_screen(MainScreen())

    @property
    def profiles_local(self) -> bool:
        return self._driver is not None and not self.is_web

    def profile_feeds(self) -> list[FeedWidget]:
        return [feed for screen in self.screen_stack for feed in screen.query(FeedWidget)]

    @property
    def profiles_editable(self) -> bool:
        if not self.profiles_local or self._profile_error or self._profile_path is None:
            return False
        for feed in self.profile_feeds():
            if feed._sdr_replacement_request is not None:  # noqa: SLF001
                return False
            if feed.sdr_session is not None and feed.sdr_session.state in (
                'starting',
                'running',
                'stopping',
                'cleanup_failed',
            ):
                return False
            for worker in (feed._sdr_start_worker, feed._sdr_stop_worker):  # noqa: SLF001
                if worker is not None and not worker.is_finished:
                    return False
        return True

    def refresh_profile_views(self) -> None:
        for screen in self.screen_stack:
            if isinstance(screen, ProfilesScreen) and screen.is_mounted:
                screen.refresh_profiles()

    def _apply_profile_selection(self) -> None:
        selected = self.profile_catalog.selected
        self.sdr_config = selected.config if selected else None
        self.profile_source = f'Profile: {selected.name}' if selected else 'none'
        for feed in self.profile_feeds():
            state = feed.sdr_session.state if feed.sdr_session else 'stopped'
            feed.query_one(f'#sdr-start-{feed.port}', Button).disabled = (
                state in ('starting', 'running', 'stopping', 'cleanup_failed') or self.sdr_config is None
            )

    def _commit_profiles(self, catalog: ProfileCatalog, *, apply_selection: bool) -> bool:
        if not self.profiles_editable:
            self.profile_notice = 'Profiles are unavailable or SDR cleanup is still pending'
            return False
        try:
            save_profiles(self._profile_path, catalog)
        except ProfileStoreError as exc:
            self.profile_notice = str(exc)
            if not exc.committed:
                self.refresh_profile_views()
                return False
            try:
                catalog = load_profiles(self._profile_path)
            except ProfileStoreError as load_error:
                self._profile_error = True
                self.sdr_config = None
                self.profile_source = 'unavailable: committed storage cannot be read'
                self.profile_notice = f'{exc}; {load_error}'
                self.refresh_profile_views()
                return False
        else:
            self.profile_notice = 'Profiles saved'
        self.profile_catalog = catalog
        if apply_selection:
            self._apply_profile_selection()
        self.refresh_profile_views()
        return True

    def save_profile(self, profile: FrequencyProfile, original_name: str | None = None) -> bool:
        if not self.profiles_editable:
            return False
        profiles = list(self.profile_catalog.profiles)
        selected = self.profile_catalog.selected_name
        if original_name is None:
            profiles.append(profile)
        else:
            index = next((i for i, item in enumerate(profiles) if item.name == original_name), None)
            if index is None:
                return False
            profiles[index] = profile
            if selected == original_name:
                selected = profile.name
        try:
            catalog = ProfileCatalog(tuple(profiles), selected)
        except (ValueError, TypeError) as exc:
            self.profile_notice = str(exc)
            return False
        return self._commit_profiles(
            catalog, apply_selection=original_name is not None and (original_name == self.profile_catalog.selected_name)
        )

    def select_profile(self, name: str) -> bool:
        if not self.profiles_editable or not any(p.name == name for p in self.profile_catalog.profiles):
            return False
        return self._commit_profiles(ProfileCatalog(self.profile_catalog.profiles, name), apply_selection=True)

    def delete_profile(self, name: str) -> bool:
        if not self.profiles_editable or not any(p.name == name for p in self.profile_catalog.profiles):
            return False
        selected = self.profile_catalog.selected_name
        profiles = tuple(p for p in self.profile_catalog.profiles if p.name != name)
        catalog = ProfileCatalog(profiles, None if selected == name else selected)
        return self._commit_profiles(catalog, apply_selection=selected == name)

    def check_action(self, action: str, _parameters: tuple[object, ...]) -> bool | None:
        if action == 'sdr_profiles':
            return self.profiles_local
        return True

    def action_sdr_profiles(self) -> None:
        if self.profiles_local and isinstance(self.screen, MainScreen):
            self.push_screen(ProfilesScreen())

    def action_clear_screen(self) -> None:
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            pane.query_one(FeedWidget).clear()

    def action_about(self) -> None:
        self.push_screen(AboutScreen())

    def action_rename_tab(self) -> None:
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            self._rename_pane = pane
            title = str(pane._title) if pane._title else ''
            self.push_screen(RenameTabScreen(current_title=title))

    def action_save_as(self) -> None:
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            table = pane.query_one(DataTable)
            rows = _extract_table_rows(table)
            port = pane.query_one(FeedWidget).port
            self.push_screen(SaveScreen(rows=rows, port=port))


def _serve_mode(host: str | None, port: int) -> None:
    """Handle serve mode logic."""
    try:
        import socket

        from textual_serve.server import Server

        if host == 'localhost':
            public_url = f'http://localhost:{port}'
        else:
            public_url = f'http://{socket.getfqdn()}:{port}'
        server = Server(command='mmng-ui', host=host, port=port, public_url=public_url)
        server.serve()
    except ImportError:
        click.echo(
            'Error: textual-serve is not installed.  Please install mmng-ui via "pipx install mmng-ui[web]"', err=True
        )
        sys.exit(1)


@click.command(context_settings={'show_default': True})
@click.option('--mmng-binary', '-m', required=False, default='multimon-ng', help='Path to multimon-ng binary')
@click.option(
    '--sox-binary',
    '-s',
    required=False,
    default='sox',
    help='Path to sox binary (this does not imply that sox will run',
)
@click.option('--sox-rate', '-r', required=False, type=str, help='Input samplerate for sox to convert from')
@click.option('--sdr-frequency-hz', required=False, type=int, help='RTL-SDR receive frequency in Hz')
@click.option('--sdr-gain-db', required=False, type=float, help='RTL-SDR manual tuner gain in dB')
@click.option('--sdr-device-index', required=False, type=int, default=0, help='RTL-SDR device index')
@click.option(
    '--port',
    '-p',
    required=False,
    default='8888',
    help='Port(s) to listen on (comma-separated); append =Name or :Name for custom tab name',
)
@click.option(
    '--charset',
    '-c',
    type=click.Choice(['US', 'FR', 'DE', 'SE', 'SI'], case_sensitive=False),
    required=False,
    default='US',
    help='Charset encoding (case sensitive!)',
)
@click.option('--capcodes', '-k', required=False, type=str, help='Path to capcode database (JSON or CSV)')
@click.option('--serve', required=False, is_flag=True, default=False, help='Serve the app via the web')
@click.option('--serve-host', required=False, type=str, help='Host/IP to serve the app on (when using --serve)')
@click.option('--serve-port', required=False, type=int, help='Port to serve the app on (when using --serve)')
@click.version_option(version=__version__)
def main(
    mmng_binary,
    sox_binary,
    sox_rate,
    sdr_frequency_hz,
    sdr_gain_db,
    sdr_device_index,
    port,
    charset,
    capcodes,
    serve,
    serve_host,
    serve_port,
):
    if serve or serve_host or serve_port:
        if not serve:
            serve = True
        if not serve_host:
            serve_host = None
        if not serve_port:
            serve_port = 8000
        _serve_mode(serve_host, serve_port)

    else:
        if not shutil.which(mmng_binary):
            click.echo(f'multimon-ng binary not found!  I searched for "{mmng_binary}"', err=True)
            sys.exit(1)

        if sox_rate:
            if not shutil.which(sox_binary):
                click.echo(f'sox binary not found!  I searched for "{sox_binary}"', err=True)
                sys.exit(1)

        if (sdr_frequency_hz is None) != (sdr_gain_db is None):
            message = '--sdr-frequency-hz and --sdr-gain-db must be provided together'
            raise click.UsageError(message)

        sdr_config = None
        if sdr_frequency_hz is not None:
            try:
                sdr_config = SDRConfig(sdr_frequency_hz, sdr_gain_db, sdr_device_index)
            except ValueError as exc:
                raise click.BadParameter(str(exc), param_hint='--sdr-frequency-hz/--sdr-gain-db') from exc

        port_configs = parse_ports(port)
        capcode_db = CapcodeDB.load(capcodes) if capcodes else None
        Pocsag(
            mmng_binary=mmng_binary,
            sox_binary=sox_binary,
            sox_rate=sox_rate,
            port_configs=port_configs,
            charset=charset,
            capcode_db=capcode_db,
            sdr_config=sdr_config,
        ).run()


if __name__ == '__main__':
    main()
