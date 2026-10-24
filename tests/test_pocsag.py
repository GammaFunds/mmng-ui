import asyncio
import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.text import Text
from textual.app import App, ComposeResult
from textual.drivers.headless_driver import HeadlessDriver
from textual.message import Message
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Markdown,
    RadioButton,
    RadioSet,
    TabbedContent,
    TabPane,
)

import mmng_ui.pocsag as pocsag_module
from mmng_ui.capcode_db import CapcodeDB
from mmng_ui.pocsag import (
    AboutScreen,
    Executable,
    FeedTabbedContent,
    FeedWidget,
    HelpScreen,
    MainScreen,
    MsgsPerSecond,
    OutputMessage,
    Pocsag,
    PortConfig,
    ProfilesScreen,
    RenameTabScreen,
    SaveScreen,
    Status,
    StatusWidget,
    UDPHandler,
    _extract_table_rows,
    _format_as_csv,
    _format_as_json,
    _format_as_markdown,
    _serve_mode,
    parse_ports,
)
from mmng_ui.sdr import DeviceBusyError, RTLFMReceiver, SDRConfig, SdrError
from mmng_ui.sdr_profiles import (
    FrequencyProfile,
    ProfileCatalog,
    ProfileStoreError,
    default_profile_path,
    load_profiles,
    save_profiles,
)


class StatusApp(App):
    def compose(self) -> ComposeResult:
        yield StatusWidget(id='test-status')


def test_output_message():
    """Test OutputMessage dataclass."""
    msg = OutputMessage('test output')
    assert isinstance(msg, Message)
    assert msg.output == 'test output'


def test_status_dataclass():
    """Test Status dataclass."""
    status = Status(receiver='test_receiver', ip_address='192.168.1.1', json_mode=True, charset='US')
    assert status.receiver == 'test_receiver'
    assert status.ip_address == '192.168.1.1'
    assert status.json_mode is True
    assert status.charset == 'US'

    # Test __repr__
    repr_str = repr(status)
    assert 'Receiver: test_receiver' in repr_str
    assert 'IP address: 192.168.1.1' in repr_str


def test_executable_dataclass():
    """Test Executable dataclass."""
    # Test with resolved_path
    exec_with_path = Executable(command='test_cmd', resolved_path='/usr/bin/test_cmd', version='1.0.0')
    assert exec_with_path.command == 'test_cmd'
    assert exec_with_path.resolved_path == '/usr/bin/test_cmd'
    assert exec_with_path.version == '1.0.0'

    # Test with None resolved_path
    exec_none_path = Executable(command='test_cmd', resolved_path=None, version=None)
    assert exec_none_path.command == 'test_cmd'
    assert exec_none_path.resolved_path is None
    assert exec_none_path.version is None


@pytest.mark.asyncio
async def test_status_widget_render():
    """Test StatusWidget render method."""
    async with StatusApp().run_test() as pilot:
        status = pilot.app.query_one('#test-status')

        # Check default render output
        rendered = status.render()
        assert 'Receiver: [dark_red]Not connected[/]' in rendered
        assert 'IP address: [wheat4]None[/]' in rendered
        assert 'JSON mode: [wheat4]Unknown[/]' in rendered
        assert 'Charset: [wheat4]Unknown[/]' in rendered

        # Change reactive attributes and check render updates
        status.receiver = '[green]receiving[/]'
        status.ip_address = '[blue]192.168.1.100[/]'
        status.json_mode = '[yellow]Yes[/]'
        status.charset = '[cyan]FR[/]'

        rendered = status.render()
        assert 'Receiver: [green]receiving[/]' in rendered
        assert 'IP address: [blue]192.168.1.100[/]' in rendered
        assert 'JSON mode: [yellow]Yes[/]' in rendered
        assert 'Charset: [cyan]FR[/]' in rendered


# HelpScreen tests
@pytest.mark.asyncio
async def test_help_screen_compose():
    """Test HelpScreen composes correctly."""
    async with App().run_test() as pilot:
        pilot.app.push_screen(HelpScreen())
        await pilot.pause()
        help_widget = pilot.app.screen.query_one('#help')
        assert isinstance(help_widget, Markdown)


@pytest.mark.asyncio
async def test_help_screen_dismiss_escape():
    """Test HelpScreen dismisses with escape key."""
    async with App().run_test() as pilot:
        pilot.app.push_screen(HelpScreen())
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_help_screen_dismiss_q():
    """Test HelpScreen dismisses with q key."""
    async with App().run_test() as pilot:
        pilot.app.push_screen(HelpScreen())
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press('q')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_help_screen_dismiss_space():
    """Test HelpScreen dismisses with space key."""
    async with App().run_test() as pilot:
        pilot.app.push_screen(HelpScreen())
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press('space')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, HelpScreen)


# AboutScreen tests
class AboutScreenApp(App):
    CSS = Path(pocsag_module.__file__).with_name('pocsag.tcss').read_text()

    def __init__(self, has_sox: bool = False):
        self.mmng = Executable('multimon-ng', '/usr/bin/multimon-ng', '1.4.0')
        self.sox_rate = 48000 if has_sox else None
        if has_sox:
            self.sox = Executable('sox', '/usr/bin/sox', '14.4.2')
        super().__init__()


def rendered_widget_text(screen, widget):
    region = widget.region
    strips = screen._compositor.render_strips()  # noqa: SLF001
    return ''.join(strip.text[region.x : region.right] for strip in strips[region.y : region.bottom])


@pytest.mark.asyncio
@pytest.mark.parametrize('size', [(80, 24), (120, 30)])
@pytest.mark.parametrize('has_sox', [False, True])
async def test_about_dialog_production_layout_and_readable_versions(size, has_sox):
    long_path = '/synthetic/build/toolchain/' + 'decoder-installation/' * 6 + 'bin/multimon-ng'
    app = AboutScreenApp(has_sox=has_sox)
    app.mmng.resolved_path = long_path
    app.mmng.version = '1.6.2'

    async with app.run_test(size=size) as pilot:
        app.push_screen(AboutScreen())
        await pilot.pause()
        dialog = app.screen.query_one('#about')
        versions = app.screen.query_one('#app-versions')
        decoder_path = app.screen.query_one('#decoder-path', Label)
        rendered = '\n'.join(strip.text for strip in app.screen._compositor.render_strips())  # noqa: SLF001
        visible_text = rendered.replace(' ', '').replace('\n', '')
        decoder_path_text = rendered_widget_text(app.screen, decoder_path)
        dialog_width = dialog.region.width
        dialog_x = dialog.region.x
        dialog_right = dialog.region.right
        versions_width = versions.region.width
        versions_right = versions.region.right
        decoder_width = versions.children[0].region.width
        decoder_path_right = decoder_path.region.right

        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(app.screen, AboutScreen)
        assert dialog_width >= int(size[0] * 0.85)
        assert dialog_x >= 0
        assert dialog_right <= size[0]
        assert versions_width > 0
        if not has_sox:
            assert decoder_width >= int(versions_width * 0.8)
        assert decoder_path_right <= versions_right
        assert long_path.replace('/', '') in decoder_path_text.replace('Path:', '').replace(' ', '').replace('/', '')
        assert 'Version:1.6.2' in visible_text
        assert f'mmng-uiversion:{pocsag_module.__version__}' in visible_text
        if has_sox:
            assert '/usr/bin/sox' in rendered
            assert '14.4.2' in rendered


@pytest.mark.asyncio
async def test_about_screen_without_sox():
    """Test AboutScreen without sox shows mmng info but not sox info."""
    async with AboutScreenApp(has_sox=False).run_test() as pilot:
        pilot.app.push_screen(AboutScreen())
        await pilot.pause()
        about_screen = pilot.app.screen
        version_label = about_screen.query_one('#about-version', Label)
        assert pocsag_module.__version__ in str(version_label.render())
        versions = about_screen.query_one('#app-versions')
        assert len(versions.children) == 1
        mmng_path = about_screen.query_one('#decoder-path', Label)
        mmng_version = about_screen.query_one('#decoder-version', Label)
        assert isinstance(mmng_path, Label)
        assert isinstance(mmng_version, Label)
        assert '/usr/bin/multimon-ng' in str(mmng_path.render())
        assert '1.4.0' in str(mmng_version.render())


@pytest.mark.asyncio
async def test_about_screen_with_sox():
    """Test AboutScreen with sox shows both mmng and sox info."""
    async with AboutScreenApp(has_sox=True).run_test() as pilot:
        pilot.app.push_screen(AboutScreen())
        await pilot.pause()
        about_screen = pilot.app.screen
        version_label = about_screen.query_one('#about-version', Label)
        assert pocsag_module.__version__ in str(version_label.render())
        versions = about_screen.query_one('#app-versions')
        assert len(versions.children) == 2
        mmng_path = about_screen.query_one('#decoder-path', Label)
        mmng_version = about_screen.query_one('#decoder-version', Label)
        sox_path = about_screen.query_one('#sox-path', Label)
        sox_version = about_screen.query_one('#sox-version', Label)
        assert isinstance(mmng_path, Label)
        assert isinstance(mmng_version, Label)
        assert isinstance(sox_path, Label)
        assert isinstance(sox_version, Label)
        assert '/usr/bin/multimon-ng' in str(mmng_path.render())
        assert '1.4.0' in str(mmng_version.render())
        assert '/usr/bin/sox' in str(sox_path.render())
        assert '14.4.2' in str(sox_version.render())


@pytest.mark.asyncio
async def test_about_screen_dismiss_escape():
    """Test AboutScreen dismisses with escape key."""
    async with AboutScreenApp().run_test() as pilot:
        pilot.app.push_screen(AboutScreen())
        await pilot.pause()
        assert isinstance(pilot.app.screen, AboutScreen)
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, AboutScreen)


@pytest.mark.asyncio
async def test_about_screen_dismiss_q():
    """Test AboutScreen dismisses with q key."""
    async with AboutScreenApp().run_test() as pilot:
        pilot.app.push_screen(AboutScreen())
        await pilot.pause()
        assert isinstance(pilot.app.screen, AboutScreen)
        await pilot.press('q')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, AboutScreen)


# MainScreen compose and on_mount tests
class MainScreenTestApp(App):
    def __init__(self, ports=None, port_configs=None, capcode_db=None):
        super().__init__()
        self.mmng = Executable('multimon-ng', '/usr/bin/multimon-ng', '1.4.0')
        self.mmng_binary = 'multimon-ng'
        self.sox_binary = None
        self.sox_rate = None
        self.charset = 'US'
        if port_configs is not None:
            self.port_configs = port_configs
        else:
            self.port_configs = [PortConfig(p) for p in (ports or [8888])]
        self.message_count = []
        self.json_capable = True
        self.mmng.version = '1.4.0'
        self._streaming_disabled = True
        self.capcode_db = capcode_db

    def on_mount(self) -> None:
        self.push_screen(MainScreen())


@pytest.mark.asyncio
async def test_main_screen_compose():
    """Test MainScreen composes Header, Footer, and TabbedContent with FeedWidgets."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        header = pilot.app.screen.query_one(Header)
        footer = pilot.app.screen.query_one(Footer)
        tab_content = pilot.app.screen.query_one(TabbedContent)
        assert header is not None
        assert footer is not None
        assert tab_content is not None

        messages_table = pilot.app.screen.query_one('#messages-8888')
        log_widget = pilot.app.screen.query_one('#log-8888')
        status_widget = pilot.app.screen.query_one('#status-8888')
        sparkline = pilot.app.screen.query_one('#spark-8888')
        mps = pilot.app.screen.query_one('#msgs-per-second-8888')
        assert messages_table is not None
        assert log_widget is not None
        assert status_widget is not None
        assert sparkline is not None
        assert mps is not None


@pytest.mark.asyncio
async def test_main_screen_on_mount_sets_columns():
    """Test MainScreen.on_mount sets up DataTable columns correctly."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        assert 'time' in table.columns
        assert 'address' in table.columns
        assert 'message' in table.columns
        assert str(table.columns['time'].label) == 'Time'
        assert str(table.columns['address'].label) == 'Address'
        assert str(table.columns['message'].label) == 'Message'


# MainScreen on_output_message tests
@pytest.mark.asyncio
async def test_main_screen_on_output_message_adds_row():
    """Test FeedWidget adds row to DataTable for valid message."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        test_line = '2024-09-23 12:38:00: POCSAG512: Address:  162202  Function: 0  Alpha:   test message'
        feed = pilot.app.screen.query_one(FeedWidget)
        feed.post_message(OutputMessage(test_line))
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        assert len(list(table.rows)) == 1
        row_key = list(table.rows.keys())[0]
        cells = table.get_row(row_key)
        assert cells[2] == Text('test message', overflow='fold')


@pytest.mark.asyncio
async def test_main_screen_on_output_message_no_message():
    """Test FeedWidget logs warning for invalid message."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        feed.post_message(OutputMessage('garbage input'))
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        assert len(list(table.rows)) == 0


@pytest.mark.asyncio
async def test_on_output_message_shows_capcode_alias(tmp_path):
    """Test FeedWidget shows alias for known capcode address."""
    json_file = tmp_path / 'test_capcodes.json'
    json_file.write_text(
        '{"data": [{"address": "1622020", "alias": "Test Alias", "agency": "TA", "color": "purple", "icon": "fire"}]}'
    )
    capcode_db = CapcodeDB.load(json_file)
    async with MainScreenTestApp(capcode_db=capcode_db).run_test() as pilot:
        await pilot.pause()
        test_line = '2024-09-23 12:38:00: POCSAG512: Address:  162202  Function: 0  Alpha:   test message'
        feed = pilot.app.screen.query_one(FeedWidget)
        feed.post_message(OutputMessage(test_line))
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        assert len(list(table.rows)) == 1
        row_key = list(table.rows.keys())[0]
        cells = table.get_row(row_key)
        assert 'Test Alias' in str(cells[1])
        assert '1622020' in str(cells[1])
        assert '🔥' in str(cells[1])


@pytest.mark.asyncio
async def test_on_output_message_unknown_capcode_shows_raw(tmp_path):
    """Test FeedWidget shows raw address for unknown capcode."""
    json_file = tmp_path / 'test_capcodes.json'
    json_file.write_text('{"data": [{"address": "9999999", "alias": "Other", "agency": "XX"}]}')
    capcode_db = CapcodeDB.load(json_file)
    async with MainScreenTestApp(capcode_db=capcode_db).run_test() as pilot:
        await pilot.pause()
        test_line = '2024-09-23 12:38:00: POCSAG512: Address:  162202  Function: 0  Alpha:   test message'
        feed = pilot.app.screen.query_one(FeedWidget)
        feed.post_message(OutputMessage(test_line))
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        assert len(list(table.rows)) == 1
        row_key = list(table.rows.keys())[0]
        cells = table.get_row(row_key)
        assert '1622020' in str(cells[1])
        assert 'Other' not in str(cells[1])


# Pocsag action and key binding tests
class ActionTestApp(App):
    """A minimal App with Pocsag bindings/actions and a MainScreen."""

    CSS_PATH = None
    BINDINGS = [
        ('c', 'clear_screen', 'Clear'),
        ('question_mark', "app.push_screen('help')", 'Help'),
        ('a', 'about', 'About'),
        ('q', 'quit', 'Quit'),
        ('r', 'rename_tab', 'Rename tab'),
    ]
    SCREENS = {'help': HelpScreen, 'about': AboutScreen}

    def __init__(self, ports=None, port_configs=None):
        super().__init__()
        self.mmng = Executable('multimon-ng', '/usr/bin/multimon-ng', '1.4.0')
        self.mmng_binary = 'multimon-ng'
        self.sox_binary = None
        self.sox_rate = None
        self.charset = 'US'
        if port_configs is not None:
            self.port_configs = port_configs
        else:
            self.port_configs = [PortConfig(p) for p in (ports or [8888])]
        self.message_count = []
        self.json_capable = True
        self.mmng.version = '1.4.0'
        self._streaming_disabled = True
        self.capcode_db = None

    def action_clear_screen(self):
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            pane.query_one(FeedWidget).clear()

    def action_about(self):
        self.push_screen('about')

    def action_rename_tab(self):
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            self._rename_pane = pane
            title = str(pane._title) if pane._title else ''
            self.push_screen(RenameTabScreen(current_title=title))

    def on_mount(self) -> None:
        self.push_screen(MainScreen())


@pytest.mark.asyncio
async def test_action_clear_screen():
    """Test action_clear_screen clears DataTable and RichLog."""
    async with ActionTestApp().run_test() as pilot:
        await pilot.pause()

        table = pilot.app.screen.query_one('#messages-8888')
        table.add_row('12:00:00', '123456', 'test message')
        await pilot.pause()

        assert len(list(table.rows)) == 1

        await pilot.press('c')
        await pilot.pause()

        assert len(list(table.rows)) == 0

        log = pilot.app.screen.query_one('#log-8888')
        assert len(log.lines) == 0


@pytest.mark.asyncio
async def test_key_binding_help():
    """Test ? key binding opens HelpScreen."""
    async with ActionTestApp().run_test() as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, HelpScreen)
        await pilot.press('?')
        await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_key_binding_about():
    """Test a key binding opens AboutScreen."""
    async with ActionTestApp().run_test() as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, AboutScreen)
        await pilot.press('a')
        await pilot.pause()
        assert isinstance(pilot.app.screen, AboutScreen)


@pytest.mark.asyncio
async def test_key_binding_quit():
    """Test q key binding quits the app."""
    async with ActionTestApp().run_test() as pilot:
        await pilot.pause()
        await pilot.press('q')
        await pilot.pause()
        assert not pilot.app.is_running


# Tests for MsgsPerSecond.update_graph
class MsgsPerSecondApp(App):
    def __init__(self):
        super().__init__()
        self.message_count = []

    def compose(self) -> ComposeResult:
        yield MsgsPerSecond(message_count_ref=self.message_count)


@pytest.mark.asyncio
async def test_msgspersecond_update_graph_appends_count():
    """Test MsgsPerSecond.update_graph appends count and resets message_count."""
    async with MsgsPerSecondApp().run_test() as pilot:
        mps = pilot.app.screen.query_one(MsgsPerSecond)
        pilot.app.message_count.extend([10, 20, 30])
        mps.update_graph()
        assert mps.data[-1] == 3
        assert pilot.app.message_count == []


@pytest.mark.asyncio
async def test_msgspersecond_update_graph_keeps_last_60():
    """Test MsgsPerSecond.update_graph keeps only last 60 elements."""
    async with MsgsPerSecondApp().run_test() as pilot:
        mps = pilot.app.screen.query_one(MsgsPerSecond)
        pilot.app.message_count.extend(range(70))
        mps.update_graph()
        assert len(mps.data) <= 60
        assert mps.data[-1] == 70


# Tests for MainScreen.read_process_output
class MockStreamReader:
    def __init__(self, lines: list[bytes]):
        self._lines = lines
        self._idx = 0

    async def readline(self):
        if self._idx < len(self._lines):
            line = self._lines[self._idx]
            self._idx += 1
            return line
        return b''


@pytest.mark.asyncio
async def test_read_process_output_yields_decoded_lines():
    """Test FeedWidget.read_process_output yields decoded lines."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        mock_stream = MockStreamReader([b'hello world\n', b''])
        results = []
        async for line in feed.read_process_output(mock_stream):
            results.append(line)
        assert results == ['hello world']


@pytest.mark.asyncio
async def test_read_process_output_breaks_on_empty():
    """Test FeedWidget.read_process_output breaks on empty line."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        mock_stream = MockStreamReader([b''])
        results = []
        async for line in feed.read_process_output(mock_stream):
            results.append(line)
        assert results == []


@pytest.mark.asyncio
async def test_read_process_output_sets_status_receiver():
    """Test FeedWidget.read_process_output sets status receiver."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        status = feed.query_one('#status-8888')
        mock_stream = MockStreamReader([b'test line\n', b''])
        async for _ in feed.read_process_output(mock_stream):
            pass
        assert status.receiver == '[blink bold bright_green]receiving[/]'


# Tests for MainScreen.on_output_message scroll
@pytest.mark.asyncio
async def test_on_output_message_scrolls_to_bottom():
    """Test FeedWidget scrolls to bottom after adding a row."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        test_line = '2024-09-23 12:38:00: POCSAG512: Address:  162202  Function: 0  Alpha:   test message'
        feed = pilot.app.screen.query_one(FeedWidget)
        feed.post_message(OutputMessage(test_line))
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        assert len(list(table.rows)) == 1
        assert table.scroll_y == table.max_scroll_y


# Tests for _serve_mode
def test_serve_mode_starts_server(monkeypatch):
    """Test _serve_mode starts server with correct parameters."""
    server_args = {}

    class MockServer:
        def __init__(self, command, host, port, public_url):
            server_args.update(command=command, host=host, port=port, public_url=public_url)

        def serve(self):
            server_args['served'] = True

    mock_module = type(sys)('textual_serve')
    mock_module.server = type(sys)('server')
    mock_module.server.Server = MockServer
    monkeypatch.setitem(sys.modules, 'textual_serve', mock_module)
    monkeypatch.setitem(sys.modules, 'textual_serve.server', mock_module.server)
    monkeypatch.setattr('socket.getfqdn', lambda: 'myhost')

    _serve_mode('0.0.0.0', 8000)
    assert server_args['command'] == 'mmng-ui'
    assert server_args['host'] == '0.0.0.0'
    assert server_args['port'] == 8000
    assert server_args['public_url'] == 'http://myhost:8000'
    assert server_args['served'] is True

    # Test with localhost
    server_args.clear()
    _serve_mode('localhost', 8000)
    assert server_args['public_url'] == 'http://localhost:8000'


def test_serve_mode_missing_dependency_exits(monkeypatch):
    """Test _serve_mode exits when textual-serve is missing."""

    def mock_import(name, *args, **kwargs):
        if name == 'textual_serve.server':
            raise ImportError("No module named 'textual_serve'")
        return importlib.__import__(name, *args, **kwargs)

    echo_messages = []

    def mock_echo(message, err=False):
        echo_messages.append(message)

    exit_code = []

    def mock_exit(code):
        exit_code.append(code)

    monkeypatch.setattr('builtins.__import__', mock_import)
    monkeypatch.setattr('click.echo', mock_echo)
    monkeypatch.setattr('sys.exit', mock_exit)

    _serve_mode(None, 8000)

    assert len(echo_messages) == 1
    assert 'textual-serve is not installed' in echo_messages[0]
    assert exit_code == [1]


# --- parse_ports tests ---


def test_parse_ports_single():
    """Test parse_ports with a single port."""
    assert parse_ports('8888') == [PortConfig(8888)]


def test_parse_ports_single_with_equals():
    """Test parse_ports with a single port and equals syntax."""
    assert parse_ports('8888=Cops') == [PortConfig(8888, 'Cops')]


def test_parse_ports_single_with_colon():
    """Test parse_ports with a single port and colon syntax."""
    assert parse_ports('8888:FireDept') == [PortConfig(8888, 'FireDept')]


def test_parse_ports_multiple():
    """Test parse_ports with multiple ports."""
    assert parse_ports('8888,8889,8890') == [
        PortConfig(8888),
        PortConfig(8889),
        PortConfig(8890),
    ]


def test_parse_ports_multiple_mixed():
    """Test parse_ports with multiple ports using mixed syntax."""
    assert parse_ports('8888=Cops,8889:Fire') == [
        PortConfig(8888, 'Cops'),
        PortConfig(8889, 'Fire'),
    ]


def test_parse_ports_with_spaces():
    """Test parse_ports handles spaces."""
    assert parse_ports('8888, 8889') == [PortConfig(8888), PortConfig(8889)]


def test_parse_ports_empty_name_equals():
    """Test parse_ports with empty name after equals raises ValueError."""
    with pytest.raises(ValueError):
        parse_ports('8888=')


def test_parse_ports_empty_name_colon():
    """Test parse_ports with empty name after colon raises ValueError."""
    with pytest.raises(ValueError):
        parse_ports('8888:')


def test_parse_ports_whitespace_only_name():
    """Test parse_ports with whitespace-only name raises ValueError."""
    with pytest.raises(ValueError):
        parse_ports('8888=   ')


def test_parse_ports_invalid():
    """Test parse_ports raises ValueError for invalid input."""
    with pytest.raises(ValueError):
        parse_ports('abc')


def test_parse_ports_empty():
    """Test parse_ports raises ValueError for empty string."""
    with pytest.raises(ValueError):
        parse_ports('')


@pytest.mark.asyncio
async def test_sdr_start_stop_controls_do_not_auto_start_with_configuration(monkeypatch):
    starts = []

    async def start(self):
        starts.append(self)

    monkeypatch.setattr(RTLFMReceiver, 'start', start)
    app = MainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)

    async with app.run_test() as pilot:
        await pilot.pause()
        assert not starts
        assert pilot.app.screen.query_one('#sdr-start-8888').disabled is False
        assert pilot.app.screen.query_one('#sdr-stop-8888').disabled is True


@pytest.mark.asyncio
async def test_sdr_controls_require_configuration_and_never_auto_start(monkeypatch):
    starts = []
    subprocess_calls = []

    async def start(self):
        starts.append(self)

    async def create_subprocess_exec(*args, **_kwargs):
        subprocess_calls.append(args)
        message = 'unexpected subprocess creation'
        raise AssertionError(message)

    async def detect_version(_self):
        return '1.4.0', True

    monkeypatch.setattr(RTLFMReceiver, 'start', start)
    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', lambda _feed: None)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert getattr(pilot.app, 'sdr_config', None) is None
        start = pilot.app.screen.query_one('#sdr-start-8888')
        stop = pilot.app.screen.query_one('#sdr-stop-8888')
        assert start.disabled is True
        assert stop.disabled is True
        assert pilot.app.screen.region.contains_region(start.region)
        assert pilot.app.screen.region.contains_region(stop.region)
        assert starts == []
        assert subprocess_calls == []

        feed = pilot.app.screen.query_one(FeedWidget)
        pilot.app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)
        # Exercise the state callback after configuration becomes available.
        feed._show_sdr_state('stopped', None)  # noqa: SLF001

        assert start.disabled is False
        assert stop.disabled is True
        assert starts == []
        assert subprocess_calls == []


@pytest.mark.asyncio
async def test_sdr_buttons_start_and_stop_the_isolated_receiver(monkeypatch):
    events = []

    class Sink:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        def drain(self):
            pass

    sink = Sink()

    class FakeReceiver:
        def __init__(self, config, receiver_sink, on_state):
            events.append(('configure', config, receiver_sink))
            self.on_state = on_state
            self.state = 'stopped'

        async def start(self):
            events.append(('start',))
            self.state = 'running'
            self.on_state('running', None)

        async def stop(self):
            if self.state == 'stopped':
                return
            events.append(('stop',))
            self.state = 'stopped'
            self.on_state('stopped', None)

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=sink)

    monkeypatch.setattr(pocsag_module, 'RTLFMReceiver', FakeReceiver)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert events == []
        start = pilot.app.screen.query_one('#sdr-start-8888')
        stop = pilot.app.screen.query_one('#sdr-stop-8888')
        assert pilot.app.screen.region.contains_region(start.region)
        assert pilot.app.screen.region.contains_region(stop.region)
        assert start.disabled is False
        assert stop.disabled is True
        await pilot.click('#sdr-start-8888')
        await pilot.pause()
        assert events[0] == ('configure', app.sdr_config, sink)
        assert events[1] == ('start',)
        assert start.disabled is True
        assert stop.disabled is False

        feed = pilot.app.screen.query_one(FeedWidget)
        handler = UDPHandler.__new__(UDPHandler)
        handler.app = feed
        handler.loop = SimpleNamespace(time=lambda: 10.0)
        handler.port = 8888
        handler.status = SimpleNamespace()
        handler.last_activity_time = 0
        handler.datagram_received(b'active', ('192.0.2.12', 8888))
        assert sink.data == []

        await pilot.click('#sdr-stop-8888')
        await pilot.pause()
        assert events[-1] == ('stop',)
        assert start.disabled is False
        assert stop.disabled is True

        handler.datagram_received(b'restored', ('192.0.2.12', 8888))
        assert sink.data == [b'restored']


@pytest.mark.asyncio
@pytest.mark.parametrize('unmount_during_stop', [False, True])
async def test_textual_restart_excludes_udp_until_stop_coordinator_finishes(monkeypatch, unmount_during_stop):
    pump_finished = asyncio.Event()
    complete_stop = asyncio.Event()
    unmount_started = asyncio.Event()
    spawned = []

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()
            self.reaped = False

        async def wait(self):
            await self.exited.wait()
            self.reaped = True
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

        def kill(self):
            self.terminate()

    class Drained:
        def __await__(self):
            if False:
                yield None

    class Sink:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        def drain(self):
            return Drained()

    processes = [Process(), Process()]
    sink = Sink()

    async def spawn(*_args, **_kwargs):
        process = processes[len(spawned)]
        spawned.append(process)
        return process

    async def detect_version(_self):
        return '1.4.0', True

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=sink)

    original_terminate = RTLFMReceiver._terminate_process  # noqa: SLF001
    original_unmount = FeedWidget.on_unmount
    held_once = False

    async def pause_stop_after_pump(receiver):
        nonlocal held_once
        await original_terminate(receiver)
        if not held_once:
            held_once = True
            await asyncio.wait_for(asyncio.shield(receiver._task), timeout=1)  # noqa: SLF001
            pump_finished.set()
            await complete_stop.wait()

    async def observe_unmount(feed):
        unmount_started.set()
        await original_unmount(feed)

    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)
    monkeypatch.setattr(FeedWidget, 'on_unmount', observe_unmount)
    monkeypatch.setattr(RTLFMReceiver, '_terminate_process', pause_stop_after_pump)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(123_456_789, 12)
    stop_click = removal = None
    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            feed = app.screen.query_one(FeedWidget)
            await asyncio.wait_for(pilot.click('#sdr-start-8888'), timeout=1)
            receiver = feed.sdr_session
            stop_click = asyncio.create_task(pilot.click('#sdr-stop-8888'))
            await asyncio.wait_for(pump_finished.wait(), timeout=1)
            stop_worker = feed._sdr_stop_worker  # noqa: SLF001
            assert receiver._task.done()  # noqa: SLF001
            assert processes[0].reaped
            assert receiver.state == 'stopping'
            assert app.screen.query_one('#sdr-start-8888').disabled is True
            assert 'stopping' in app.screen.query_one('#sdr-state-8888').content
            assert receiver._device_owners[0] is receiver  # noqa: SLF001

            handler = UDPHandler.__new__(UDPHandler)
            handler.app = feed
            handler.loop = SimpleNamespace(time=lambda: 10.0)
            handler.status = SimpleNamespace()
            handler.last_activity_time = 0
            handler.datagram_received(b'pending-stop', ('192.0.2.12', 8888))
            assert sink.data == []
            await asyncio.wait_for(pilot.click('#sdr-start-8888'), timeout=1)
            assert len(spawned) == 1
            competitor = RTLFMReceiver(app.sdr_config, sink)
            with pytest.raises(DeviceBusyError):
                await competitor.start()

            if unmount_during_stop:
                removal = asyncio.ensure_future(feed.remove())
                await asyncio.wait_for(unmount_started.wait(), timeout=1)
                assert not removal.done()
            complete_stop.set()
            await asyncio.wait_for(stop_worker.wait(), timeout=1)
            await asyncio.wait_for(stop_click, timeout=1)
            if removal is not None:
                await asyncio.wait_for(removal, timeout=1)
            assert receiver.state == 'stopped'
            assert 0 not in receiver._device_owners  # noqa: SLF001
            if not unmount_during_stop:
                assert app.screen.query_one('#sdr-start-8888').disabled is False
                await asyncio.wait_for(pilot.click('#sdr-start-8888'), timeout=1)
                assert receiver.state == 'running'
                assert receiver.process is processes[1]
                assert receiver._device_owners[0] is receiver  # noqa: SLF001
                handler.datagram_received(b'restarted', ('192.0.2.12', 8888))
                assert sink.data == []
                with pytest.raises(DeviceBusyError):
                    await competitor.start()
        finally:
            complete_stop.set()
            pending = [task for task in (stop_click, removal) if task is not None]
            await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=2)


@pytest.mark.asyncio
async def test_textual_stop_event_is_processed_during_delayed_sdr_spawn(monkeypatch):
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()
    stop_processed = asyncio.Event()
    states = []

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()
            self.wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            await self.exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

        def kill(self):
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    class Sink:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    process = Process()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    async def detect_version(_self):
        return '1.4.0', True

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=Sink())

    original_show_state = FeedWidget._show_sdr_state  # noqa: SLF001

    def show_state(feed, state, error):
        states.append(state)
        if state == 'stopping':
            stop_processed.set()
        original_show_state(feed, state, error)

    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)
    monkeypatch.setattr(FeedWidget, '_show_sdr_state', show_state)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)
    start_click = stop_click = None

    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            assert states == []
            assert pilot.app.screen.query_one('#sdr-state-8888').content == 'RTL-SDR process: stopped'

            start_click = asyncio.create_task(pilot.click('#sdr-start-8888'))
            await asyncio.wait_for(spawn_started.wait(), timeout=1)
            assert 'starting' in states
            assert pilot.app.screen.query_one('#sdr-start-8888').region.width > 0

            async def interactive_pause(delay=None):
                if delay is not None:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0)

            pilot.pause = interactive_pause

            stop_click = asyncio.create_task(pilot.click('#sdr-stop-8888'))
            await asyncio.wait_for(stop_processed.wait(), timeout=0.2)
            assert pilot.app.screen.query_one('#sdr-state-8888').content == 'RTL-SDR process: stopping (UDP paused)'

            allow_spawn.set()
            await asyncio.wait_for(asyncio.gather(start_click, stop_click), timeout=1)
            feed = pilot.app.screen.query_one(FeedWidget)
            assert 'running' not in states
            assert process.returncode == -15
            assert process.wait_calls >= 1
            assert feed.sdr_session.state == 'stopped'
            assert feed.sdr_session._task.done()  # noqa: SLF001
            assert 0 not in feed.sdr_session._device_owners  # noqa: SLF001
            assert states[-1] == 'stopped'
            assert feed._sdr_start_worker is None  # noqa: SLF001
        finally:
            allow_spawn.set()
            for task in (start_click, stop_click):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (start_click, stop_click) if task is not None), return_exceptions=True
            )


@pytest.mark.asyncio
async def test_widget_unmount_during_startup_cleans_child_without_running(monkeypatch):
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()
    stopping = asyncio.Event()
    states = []

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()
            self.wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            await self.exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

        def kill(self):
            self.terminate()

    class Sink:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    process = Process()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    async def detect_version(_self):
        return '1.4.0', True

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=Sink())

    original_show_state = FeedWidget._show_sdr_state  # noqa: SLF001

    def show_state(feed, state, error):
        states.append(state)
        if state == 'stopping':
            stopping.set()
        original_show_state(feed, state, error)

    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)
    monkeypatch.setattr(FeedWidget, '_show_sdr_state', show_state)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)
    start_click = remove_task = None

    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            feed = pilot.app.screen.query_one(FeedWidget)
            start_click = asyncio.create_task(pilot.click('#sdr-start-8888'))
            await asyncio.wait_for(spawn_started.wait(), timeout=1)

            async def interactive_pause(delay=None):
                if delay is not None:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0)

            pilot.pause = interactive_pause
            remove_task = asyncio.ensure_future(feed.remove())
            await asyncio.wait_for(stopping.wait(), timeout=0.2)
            assert not remove_task.done()
            allow_spawn.set()
            await asyncio.wait_for(asyncio.gather(start_click, remove_task), timeout=1)

            assert process.returncode == -15
            assert process.wait_calls >= 1
            assert feed.sdr_session.state == 'stopped'
            assert 'running' not in states
            assert 0 not in feed.sdr_session._device_owners  # noqa: SLF001
        finally:
            allow_spawn.set()
            for task in (start_click, remove_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (start_click, remove_task) if task is not None), return_exceptions=True
            )


@pytest.mark.asyncio
async def test_cleanup_failure_during_widget_shutdown_keeps_udp_gated(monkeypatch):
    process = None

    class PermissionDeniedProcess:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()
            self.allow_cleanup = False

        async def wait(self):
            await self.exited.wait()
            return self.returncode

        def terminate(self):
            if not self.allow_cleanup:
                raise PermissionError('shutdown signal denied')  # noqa: EM101, TRY003
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

        def kill(self):
            raise PermissionError('shutdown kill denied')  # noqa: EM101, TRY003

    class Sink:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        async def drain(self):
            pass

    sink = Sink()

    async def create_subprocess_exec(*_args, **_kwargs):
        nonlocal process
        process = PermissionDeniedProcess()
        return process

    async def detect_version(_self):
        return '1.4.0', True

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=sink)

    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        await pilot.click('#sdr-start-8888')
        receiver = feed.sdr_session
        assert receiver.state == 'running'

        await feed.remove()
        assert process.returncode is None
        assert receiver.state == 'cleanup_failed'
        assert receiver.error == 'shutdown signal denied'
        assert receiver._device_owners[0] is receiver  # noqa: SLF001

        handler = UDPHandler.__new__(UDPHandler)
        handler.app = feed
        handler.loop = SimpleNamespace(time=lambda: 10.0)
        handler.port = 8888
        handler.status = SimpleNamespace()
        handler.last_activity_time = 0
        handler.datagram_received(b'must-stay-gated', ('192.0.2.12', 8888))
        assert sink.data == []

        process.allow_cleanup = True
        await receiver.stop()
        assert receiver.state == 'stopped'


@pytest.mark.asyncio
async def test_pending_start_cleanup_failure_keeps_stop_retry_available(monkeypatch):
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()
    stopping = asyncio.Event()
    stopped = asyncio.Event()
    start_worker_finished = asyncio.Event()
    stop_worker_finished = asyncio.Event()
    stop_sdr_waiting = asyncio.Event()
    allow_stop_worker = asyncio.Event()
    stop_worker_handles = []
    states = []

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

        def terminate(self):
            if self.returncode is None:
                raise PermissionError('temporary termination denial')  # noqa: EM101, TRY003

        def kill(self):
            raise PermissionError('temporary kill denial')  # noqa: EM101, TRY003

        def finish_cleanup(self):
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    class Sink:
        def write(self, _data):
            pass

        async def drain(self):
            pass

    process = Process()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    async def detect_version(_self):
        return '1.4.0', True

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=Sink())

    original_show_state = FeedWidget._show_sdr_state  # noqa: SLF001

    def show_state(feed, state, error):
        states.append(state)
        if state == 'stopping':
            stopping.set()
        elif state == 'stopped':
            stopped.set()
        original_show_state(feed, state, error)

    original_run_start = FeedWidget._run_sdr_start  # noqa: SLF001
    original_run_stop = FeedWidget._run_sdr_stop  # noqa: SLF001
    original_stop_sdr = FeedWidget.stop_sdr
    hold_first_stop_result = True

    async def observe_start_worker(feed):
        try:
            await original_run_start(feed)
        finally:
            start_worker_finished.set()

    async def observe_stop_worker(feed):
        stop_worker_handles.append(feed._sdr_stop_worker)  # noqa: SLF001
        try:
            await original_run_stop(feed)
        finally:
            stop_worker_finished.set()

    async def hold_stop_result(feed):
        nonlocal hold_first_stop_result
        try:
            await original_stop_sdr(feed)
        finally:
            if hold_first_stop_result:
                hold_first_stop_result = False
                stop_sdr_waiting.set()
                await allow_stop_worker.wait()

    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)
    monkeypatch.setattr(FeedWidget, '_show_sdr_state', show_state)
    monkeypatch.setattr(FeedWidget, '_run_sdr_start', observe_start_worker)
    monkeypatch.setattr(FeedWidget, '_run_sdr_stop', observe_stop_worker)
    monkeypatch.setattr(FeedWidget, 'stop_sdr', hold_stop_result)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)
    start_click = stop_click = None

    async with app.run_test(size=(80, 24)) as pilot:
        try:
            await pilot.pause()
            start_click = asyncio.create_task(pilot.click('#sdr-start-8888'))
            await asyncio.wait_for(spawn_started.wait(), timeout=1)

            async def interactive_pause(delay=None):
                if delay is not None:
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(0)

            pilot.pause = interactive_pause
            stop_click = asyncio.create_task(pilot.click('#sdr-stop-8888'))
            await asyncio.wait_for(stopping.wait(), timeout=0.2)
            allow_spawn.set()
            await asyncio.wait_for(start_worker_finished.wait(), timeout=1)
            await asyncio.wait_for(stop_sdr_waiting.wait(), timeout=1)

            feed = pilot.app.screen.query_one(FeedWidget)
            receiver = feed.sdr_session
            start = pilot.app.screen.query_one('#sdr-start-8888')
            stop = pilot.app.screen.query_one('#sdr-stop-8888')
            label = pilot.app.screen.query_one('#sdr-state-8888')
            assert receiver.state == 'cleanup_failed'
            assert start.disabled is True
            assert stop.disabled is False
            assert 'cleanup_failed' in label.content
            assert process.returncode is None
            assert receiver._device_owners[0] is receiver  # noqa: SLF001

            allow_stop_worker.set()
            await asyncio.wait_for(stop_worker_finished.wait(), timeout=1)
            await asyncio.wait_for(stop_worker_handles[0].wait(), timeout=1)
            await asyncio.wait_for(asyncio.gather(start_click, stop_click), timeout=1)

            process.terminate = process.finish_cleanup
            stopped.clear()
            await asyncio.wait_for(receiver.stop(), timeout=1)
            await asyncio.wait_for(stopped.wait(), timeout=1)
            assert receiver.state == 'stopped'
            assert 0 not in receiver._device_owners  # noqa: SLF001
            assert 'running' not in states
        finally:
            allow_spawn.set()
            for task in (start_click, stop_click):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (start_click, stop_click) if task is not None),
                return_exceptions=True,
            )


@pytest.mark.asyncio
async def test_udp_resumes_only_after_stop_during_sdr_startup_cleans_up(monkeypatch):
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()
    stop_requested = asyncio.Event()

    class CompletedAwaitable:
        def __await__(self):
            if False:
                yield None

    class Sink:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        def drain(self):
            return CompletedAwaitable()

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

        def kill(self):
            self.returncode = -9
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

    process = Process()
    decoder_sink = Sink()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    async def detect_version(_self):
        return '1.4.0', True

    def start_decoder(feed):
        feed.process = SimpleNamespace(stdin=decoder_sink)

    # Observe the real state callback without replacing its UI behavior.
    original_show_state = FeedWidget._show_sdr_state  # noqa: SLF001

    def show_state(feed, state, error):
        if state == 'stopping':
            stop_requested.set()
        original_show_state(feed, state, error)

    monkeypatch.setattr(pocsag_module.asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect_version)
    monkeypatch.setattr(FeedWidget, 'start_streaming', start_decoder)
    monkeypatch.setattr(FeedWidget, '_show_sdr_state', show_state)

    class StyledMainScreenTestApp(MainScreenTestApp):
        CSS_PATH = str(Path(pocsag_module.__file__).with_name('pocsag.tcss'))

    app = StyledMainScreenTestApp()
    app.sdr_config = SDRConfig(frequency_hz=123_456_789, gain_db=12)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        assert process.returncode is None

        start_task = asyncio.create_task(feed.start_sdr())
        await asyncio.wait_for(spawn_started.wait(), timeout=1)
        handler = UDPHandler.__new__(UDPHandler)
        handler.app = feed
        handler.loop = SimpleNamespace(time=lambda: 10.0)
        handler.port = 8888
        handler.status = SimpleNamespace()
        handler.last_activity_time = 0
        handler.datagram_received(b'during-start', ('192.0.2.21', 8888))
        assert decoder_sink.data == []

        stop_task = asyncio.create_task(feed.stop_sdr())
        stop_signal_task = asyncio.create_task(stop_requested.wait())
        try:
            done, _ = await asyncio.wait_for(
                asyncio.wait({stop_task, stop_signal_task}, return_when=asyncio.FIRST_COMPLETED), timeout=1
            )
            assert stop_signal_task in done
            handler.datagram_received(b'during-stop', ('192.0.2.21', 8888))
            assert decoder_sink.data == []

            allow_spawn.set()
            await asyncio.wait_for(asyncio.gather(start_task, stop_task), timeout=1)
            assert process.returncode == -15
            assert feed.sdr_session.state == 'stopped'
            # STOPPED must include completion of the owned PCM pump.
            assert feed.sdr_session._task is None or feed.sdr_session._task.done()  # noqa: SLF001

            handler.datagram_received(b'after-cleanup', ('192.0.2.21', 8888))
            assert decoder_sink.data == [b'after-cleanup']
        finally:
            allow_spawn.set()
            if not stop_signal_task.done():
                stop_signal_task.cancel()
            await asyncio.gather(start_task, stop_task, return_exceptions=True)
            await asyncio.gather(stop_signal_task, return_exceptions=True)


def test_udp_handler_keeps_writing_raw_datagrams_to_the_decoder():
    class Stdin:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        def drain(self):
            pass

    stdin = Stdin()
    handler = UDPHandler.__new__(UDPHandler)
    handler.app = SimpleNamespace(app=SimpleNamespace(sox_rate=None), process=SimpleNamespace(stdin=stdin))
    handler.loop = SimpleNamespace(time=lambda: 10.0)
    handler.port = 8888
    handler.status = type('Status', (), {})()
    handler.last_activity_time = 0

    handler.datagram_received(b'\x00\x80', ('192.0.2.9', 8888))

    assert stdin.data == [b'\x00\x80']
    assert handler.status.ip_address == '192.0.2.9'
    assert handler.last_activity_time == 10.0


def test_udp_handler_keeps_using_sox_when_conversion_is_enabled():
    class Stdin:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        def drain(self):
            pass

    sox_stdin = Stdin()
    handler = UDPHandler.__new__(UDPHandler)
    handler.app = SimpleNamespace(
        app=SimpleNamespace(sox_rate=48000),
        sox_process=SimpleNamespace(stdin=sox_stdin),
        process=SimpleNamespace(stdin=Stdin()),
    )
    handler.loop = SimpleNamespace(time=lambda: 10.0)
    handler.port = 8888
    handler.status = SimpleNamespace()
    handler.last_activity_time = 0

    handler.datagram_received(b'\x01\x02', ('192.0.2.10', 8888))

    assert sox_stdin.data == [b'\x01\x02']
    assert handler.app.process.stdin.data == []


def test_udp_handler_does_not_mix_datagrams_into_active_sdr_audio():
    class Stdin:
        def __init__(self):
            self.data = []

        def write(self, data):
            self.data.append(data)

        def drain(self):
            pass

    stdin = Stdin()
    handler = UDPHandler.__new__(UDPHandler)
    handler.app = SimpleNamespace(
        app=SimpleNamespace(sox_rate=None),
        process=SimpleNamespace(stdin=stdin),
        sdr_session=SimpleNamespace(state='running'),
    )
    handler.loop = SimpleNamespace(time=lambda: 10.0)
    handler.port = 8888
    handler.status = SimpleNamespace()
    handler.last_activity_time = 0

    handler.datagram_received(b'\x01\x02', ('192.0.2.11', 8888))

    assert stdin.data == []


# --- FeedWidget compose tests ---


class FeedWidgetTestApp(App):
    def __init__(self):
        super().__init__()
        self.mmng = Executable('multimon-ng', '/usr/bin/multimon-ng', '1.4.0')
        self.mmng_binary = 'multimon-ng'
        self.sox_binary = None
        self.sox_rate = None
        self.charset = 'US'
        self.port_configs = [PortConfig(8888)]
        self.message_count = []
        self.json_capable = True
        self.mmng.version = '1.4.0'
        self._streaming_disabled = True


@pytest.mark.asyncio
async def test_feed_widget_compose():
    """Test FeedWidget composes all expected widgets with port-specific IDs."""
    async with FeedWidgetTestApp().run_test() as pilot:
        screen = pilot.app.screen
        feed = FeedWidget(port=8888)
        screen.mount(feed)
        await pilot.pause()

        messages_table = feed.query_one('#messages-8888')
        log_widget = feed.query_one('#log-8888')
        status_widget = feed.query_one('#status-8888')
        sparkline = feed.query_one('#spark-8888')
        mps = feed.query_one('#msgs-per-second-8888')

        assert messages_table is not None
        assert log_widget is not None
        assert status_widget is not None
        assert sparkline is not None
        assert mps is not None

        assert str(messages_table.columns['time'].label) == 'Time'
        assert str(messages_table.columns['address'].label) == 'Address'
        assert str(messages_table.columns['message'].label) == 'Message'


@pytest.mark.asyncio
async def test_feed_widget_clear():
    """Test FeedWidget.clear clears table and log."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        table = feed.query_one('#messages-8888')
        table.add_row('12:00:00', '123456', 'test message')
        log = feed.query_one('#log-8888')
        log.write('test log entry')
        await pilot.pause()

        assert len(list(table.rows)) == 1
        assert len(log.lines) == 1

        feed.clear()
        await pilot.pause()

        assert len(list(table.rows)) == 0
        assert len(log.lines) == 0


@pytest.mark.asyncio
async def test_data_table_no_blank_rows():
    """DataTable should auto-size to row count (no inline height: 100%)."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        # No inline height override — table uses default height: auto
        assert table.styles.height is None or table.styles.height.value == 1.0
        # With zero rows, no blank filler
        assert len(list(table.rows)) == 0
        # Add one row
        table.add_row('12:00:00', '123456', 'test')
        await pilot.pause()
        # Only real rows present
        assert len(list(table.rows)) == 1


@pytest.mark.asyncio
async def test_messages_container_fills_grid():
    """.feed-messages is the DataTable itself (no wrapper)."""
    async with MainScreenTestApp().run_test() as pilot:
        await pilot.pause()
        feed = pilot.app.screen.query_one(FeedWidget)
        elem = feed.query_one('.feed-messages')
        assert isinstance(elem, DataTable)
        assert elem.id == 'messages-8888'


# --- Multi-port tab tests ---


class MultiPortTestApp(App):
    BINDINGS = [
        ('c', 'clear_screen', 'Clear'),
    ]

    def __init__(self, ports=None):
        super().__init__()
        self.mmng = Executable('multimon-ng', '/usr/bin/multimon-ng', '1.4.0')
        self.mmng_binary = 'multimon-ng'
        self.sox_binary = None
        self.sox_rate = None
        self.charset = 'US'
        self.port_configs = [PortConfig(p) for p in (ports or [8888, 8889])]
        self.message_count = []
        self.json_capable = True
        self.mmng.version = '1.4.0'
        self._streaming_disabled = True
        self.capcode_db = None

    def on_mount(self) -> None:
        self.push_screen(MainScreen())

    def action_clear_screen(self):
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            pane.query_one(FeedWidget).clear()


@pytest.mark.asyncio
async def test_main_screen_multi_port_two_tabs():
    """Test MainScreen with 2 ports creates 2 tab panes."""
    async with MultiPortTestApp(ports=[8888, 8889]).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        panes = list(tabs.query(TabPane))
        assert len(panes) == 2
        assert panes[0].id == 'tab-8888'
        assert panes[1].id == 'tab-8889'

        feed1 = panes[0].query_one(FeedWidget)
        feed2 = panes[1].query_one(FeedWidget)
        assert feed1.port == 8888
        assert feed2.port == 8889

        assert panes[0].query_one('#messages-8888') is not None
        assert panes[1].query_one('#messages-8889') is not None


@pytest.mark.asyncio
async def test_main_screen_single_port_one_tab():
    """Test MainScreen with 1 port creates 1 tab pane."""
    async with MultiPortTestApp(ports=[8888]).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        panes = list(tabs.query(TabPane))
        assert len(panes) == 1
        assert panes[0].id == 'tab-8888'


@pytest.mark.asyncio
async def test_clear_only_active_tab():
    """Test action_clear_screen only clears the active tab's feed."""
    async with MultiPortTestApp(ports=[8888, 8889]).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(FeedTabbedContent)
        panes = list(tabs.query(TabPane))
        feed1 = panes[0].query_one(FeedWidget)
        feed2 = panes[1].query_one(FeedWidget)
        t1 = panes[0].query_one('#messages-8888')
        t2 = panes[1].query_one('#messages-8889')

        t1.add_row('12:00:00', '123456', 'tab1 msg')
        t2.add_row('12:00:01', '789012', 'tab2 msg')
        await pilot.pause()

        assert len(list(t1.rows)) == 1
        assert len(list(t2.rows)) == 1

        # Switch to second tab by clicking it
        await pilot.click('#--content-tab-tab-8889')
        await pilot.pause()

        await pilot.press('c')
        await pilot.pause()

        assert len(list(t2.rows)) == 0, 'active tab should be cleared'
        assert len(list(t1.rows)) == 1, 'inactive tab should not be cleared'


# --- Tab naming tests ---


@pytest.mark.asyncio
async def test_main_screen_tab_title_with_named_port():
    """Test MainScreen shows custom name for named port."""
    async with MainScreenTestApp(
        port_configs=[PortConfig(8888, 'Cops')],
    ).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        panes = list(tabs.query(TabPane))
        assert len(panes) == 1
        assert 'Cops' in str(panes[0]._title)


@pytest.mark.asyncio
async def test_main_screen_tab_title_with_unnamed_port():
    """Test MainScreen shows default 'Port X' for unnamed port."""
    async with MainScreenTestApp(
        port_configs=[PortConfig(8888)],
    ).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        panes = list(tabs.query(TabPane))
        assert len(panes) == 1
        assert 'Port 8888' in str(panes[0]._title)


@pytest.mark.asyncio
async def test_main_screen_mixed_named_unnamed_ports():
    """Test MainScreen with mix of named and unnamed ports."""
    async with MainScreenTestApp(
        port_configs=[
            PortConfig(8888, 'Cops'),
            PortConfig(8889),
            PortConfig(8890, 'Fire'),
        ],
    ).run_test() as pilot:
        await pilot.pause()
        tabs = pilot.app.screen.query_one(TabbedContent)
        panes = list(tabs.query(TabPane))
        assert len(panes) == 3
        assert 'Cops' in str(panes[0]._title)
        assert 'Port 8889' in str(panes[1]._title)
        assert 'Fire' in str(panes[2]._title)


# --- Runtime rename tests ---


@pytest.mark.asyncio
async def test_rename_tab_keybinding_opens_modal():
    """Test that pressing 'r' opens the rename tab modal."""
    async with ActionTestApp(port_configs=[PortConfig(8888, 'OldName')]).run_test() as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, RenameTabScreen)

        await pilot.press('r')
        await pilot.pause()

        assert isinstance(pilot.app.screen, RenameTabScreen)


@pytest.mark.asyncio
async def test_rename_tab_enter_confirms_change():
    """Test that entering a name and pressing Enter renames the tab."""
    async with ActionTestApp(port_configs=[PortConfig(8888, 'OldName')]).run_test() as pilot:
        await pilot.pause()

        await pilot.press('r')
        await pilot.pause()
        assert isinstance(pilot.app.screen, RenameTabScreen)

        # Type new name
        await pilot.press(*'NewName')
        await pilot.pause()

        await pilot.press('enter')
        await pilot.pause()

        assert not isinstance(pilot.app.screen, RenameTabScreen)

        tabs = pilot.app.screen.query_one(TabbedContent)
        tab = tabs.get_tab('tab-8888')
        assert 'NewName' in str(tab.label)


@pytest.mark.asyncio
async def test_rename_tab_escape_cancels():
    """Test that pressing Escape cancels the rename without changing the tab."""
    async with ActionTestApp(port_configs=[PortConfig(8888, 'OriginalName')]).run_test() as pilot:
        await pilot.pause()

        await pilot.press('r')
        await pilot.pause()
        assert isinstance(pilot.app.screen, RenameTabScreen)

        await pilot.press(*'Changed')
        await pilot.pause()

        await pilot.press('escape')
        await pilot.pause()

        assert not isinstance(pilot.app.screen, RenameTabScreen)

        tabs = pilot.app.screen.query_one(TabbedContent)
        tab = tabs.get_tab('tab-8888')
        assert 'OriginalName' in str(tab.label)


# --- SaveScreen and format tests ---

SAMPLE_ROWS = [
    {'time': '14:30:25', 'address': '1622020', 'message': 'Test message 1'},
    {'time': '14:30:26', 'address': '1234567', 'message': 'Another message with, comma'},
]

EXPECTED_CSV = (
    'Time,Address,Message\r\n'
    '14:30:25,1622020,Test message 1\r\n'
    '14:30:26,1234567,"Another message with, comma"\r\n'
)

EXPECTED_CSV_NO_CR = (
    'Time,Address,Message\n'
    '14:30:25,1622020,Test message 1\n'
    '14:30:26,1234567,"Another message with, comma"\n'
)

EXPECTED_MD = (
    '| Time | Address | Message |\n'
    '| --- | --- | --- |\n'
    '| 14:30:25 | 1622020 | Test message 1 |\n'
    '| 14:30:26 | 1234567 | Another message with, comma |\n'
)

EXPECTED_JSON = (
    '[\n'
    '  {\n'
    '    "Time": "14:30:25",\n'
    '    "Address": "1622020",\n'
    '    "Message": "Test message 1"\n'
    '  },\n'
    '  {\n'
    '    "Time": "14:30:26",\n'
    '    "Address": "1234567",\n'
    '    "Message": "Another message with, comma"\n'
    '  }\n'
    ']'
)

EXPECTED_JSON_EMPTY = '[]'

EXPECTED_CSV_HEADER_ONLY = 'Time,Address,Message\r\n'


def test_format_as_csv():
    """Test CSV formatting with multiple rows."""
    result = _format_as_csv(SAMPLE_ROWS)
    assert result == EXPECTED_CSV or result == EXPECTED_CSV_NO_CR


def test_format_as_csv_empty():
    """Test CSV formatting with empty rows."""
    result = _format_as_csv([])
    assert result == EXPECTED_CSV_HEADER_ONLY or result == 'Time,Address,Message\n'


def test_format_as_markdown():
    """Test Markdown table formatting."""
    result = _format_as_markdown(SAMPLE_ROWS)
    assert result == EXPECTED_MD


def test_format_as_markdown_empty():
    """Test Markdown formatting with empty rows returns header + separator."""
    result = _format_as_markdown([])
    assert result == EXPECTED_MD.splitlines()[0] + '\n' + EXPECTED_MD.splitlines()[1] + '\n'


def test_format_as_json():
    """Test JSON formatting with multiple rows."""
    result = _format_as_json(SAMPLE_ROWS)
    assert result == EXPECTED_JSON


def test_format_as_json_empty():
    """Test JSON formatting with empty rows."""
    result = _format_as_json([])
    assert result == EXPECTED_JSON_EMPTY


def test_format_as_json_single_row():
    """Test JSON formatting with a single row."""
    single_row = [{'time': '12:00:00', 'address': '9999999', 'message': 'Single'}]
    result = _format_as_json(single_row)
    assert '"12:00:00"' in result
    assert '"9999999"' in result
    assert '"Single"' in result


# DataTable extraction tests
class ExtractionTestApp(App):
    def compose(self) -> ComposeResult:
        table = DataTable(id='test-table')
        table.add_column('Time', key='time')
        table.add_column('Address', key='address')
        table.add_column('Message', key='message')
        table.add_row('14:30:25', '1622020', Text('Test message 1', overflow='fold'))
        table.add_row(
            '14:30:26',
            Text('Alias (1234567)', justify='right'),
            'Another message',
        )
        yield table


@pytest.mark.asyncio
async def test_extract_table_rows():
    """Test extracting rows from a DataTable."""
    async with ExtractionTestApp().run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one('#test-table')
        rows = _extract_table_rows(table)
        assert len(rows) == 2
        assert rows[0]['time'] == '14:30:25'
        assert rows[0]['address'] == '1622020'
        assert rows[0]['message'] == 'Test message 1'
        assert rows[1]['time'] == '14:30:26'
        assert rows[1]['address'] == 'Alias (1234567)'
        assert rows[1]['message'] == 'Another message'


@pytest.mark.asyncio
async def test_extract_table_rows_empty():
    """Test extracting rows from an empty DataTable."""
    async with ExtractionTestApp().run_test() as pilot:
        await pilot.pause()
        empty_table = DataTable(id='empty-table')
        empty_table.add_column('Time', key='time')
        empty_table.add_column('Address', key='address')
        empty_table.add_column('Message', key='message')
        pilot.app.screen.mount(empty_table)
        await pilot.pause()
        rows = _extract_table_rows(empty_table)
        assert rows == []


class SaveScreenTestApp(App):
    """Minimal app for testing SaveScreen."""

    def __init__(self):
        super().__init__()
        self.delivered = None
        self.delivered_filename = None

    def deliver_text(self, path_or_file, *, save_filename=None, **_kwargs):
        content = path_or_file.read() if hasattr(path_or_file, 'read') else str(path_or_file)
        self.delivered = content
        self.delivered_filename = save_filename
        return 'test-key'


@pytest.mark.asyncio
async def test_save_screen_compose():
    """Test SaveScreen composes correctly."""
    async with SaveScreenTestApp().run_test() as pilot:
        screen = SaveScreen(rows=SAMPLE_ROWS, port=8888)
        pilot.app.push_screen(screen)
        await pilot.pause()
        assert isinstance(pilot.app.screen, SaveScreen)
        assert screen.rows == SAMPLE_ROWS
        assert screen.port == 8888


@pytest.mark.asyncio
async def test_save_screen_escape_cancels():
    """Test Escape dismisses SaveScreen without saving."""
    async with SaveScreenTestApp().run_test() as pilot:
        pilot.app.push_screen(SaveScreen(rows=SAMPLE_ROWS, port=8888))
        await pilot.pause()
        assert isinstance(pilot.app.screen, SaveScreen)
        await pilot.press('escape')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, SaveScreen)
        assert pilot.app.delivered is None


@pytest.mark.asyncio
async def test_save_screen_enter_saves_csv():
    """Test Enter submits and calls deliver_text with CSV content."""
    async with SaveScreenTestApp().run_test() as pilot:
        pilot.app.push_screen(SaveScreen(rows=SAMPLE_ROWS, port=8888))
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, SaveScreen)
        assert pilot.app.delivered is not None
        assert 'Time,Address,Message' in pilot.app.delivered
        assert '14:30:25' in pilot.app.delivered
        assert pilot.app.delivered_filename is not None
        assert pilot.app.delivered_filename.startswith('mmng_port8888_')


# Integration test with full app pipeline
class SaveActionTestApp(App):
    """Test app with save_as action and binding."""

    CSS_PATH = None
    BINDINGS = [
        ('s', 'save_as', 'Save tab to file'),
    ]

    def __init__(self):
        super().__init__()
        self.mmng = Executable('multimon-ng', '/usr/bin/multimon-ng', '1.4.0')
        self.mmng_binary = 'multimon-ng'
        self.sox_binary = None
        self.sox_rate = None
        self.charset = 'US'
        self.port_configs = [PortConfig(8888)]
        self.message_count = []
        self.json_capable = True
        self.mmng.version = '1.4.0'
        self._streaming_disabled = True
        self.capcode_db = None
        self.delivered = None
        self.delivered_filename = None

    def action_save_as(self):
        tabs = self.screen.query_one(FeedTabbedContent)
        if (pane := tabs.active_pane) is not None:
            table = pane.query_one(DataTable)
            rows = _extract_table_rows(table)
            port = pane.query_one(FeedWidget).port
            self.push_screen(SaveScreen(rows=rows, port=port))

    def deliver_text(self, path_or_file, *, save_filename=None, **_kwargs):
        content = path_or_file.read() if hasattr(path_or_file, 'read') else str(path_or_file)
        self.delivered = content
        self.delivered_filename = save_filename
        return 'test-key'

    def on_mount(self):
        self.push_screen(MainScreen())


@pytest.mark.asyncio
async def test_save_action_opens_save_screen():
    """Test pressing 's' opens the SaveScreen."""
    async with SaveActionTestApp().run_test() as pilot:
        await pilot.pause()
        assert not isinstance(pilot.app.screen, SaveScreen)
        await pilot.press('s')
        await pilot.pause()
        assert isinstance(pilot.app.screen, SaveScreen)


@pytest.mark.asyncio
async def test_save_action_with_data():
    """Test saving with data in the table delivers formatted CSV."""
    async with SaveActionTestApp().run_test() as pilot:
        await pilot.pause()
        table = pilot.app.screen.query_one('#messages-8888')
        table.add_row(
            '14:30:25',
            '1622020',
            Text('Test message 1', overflow='fold'),
        )
        await pilot.pause()
        await pilot.press('s')
        await pilot.pause()
        assert isinstance(pilot.app.screen, SaveScreen)
        await pilot.press('enter')
        await pilot.pause()
        assert not isinstance(pilot.app.screen, SaveScreen)
        assert pilot.app.delivered is not None
        assert 'Time,Address,Message' in pilot.app.delivered
        assert '14:30:25' in pilot.app.delivered
        assert '1622020' in pilot.app.delivered
        assert 'Test message 1' in pilot.app.delivered


# --- SaveScreen fix tests: RadioSet default selection, extension update, notification ---


@pytest.mark.asyncio
async def test_save_screen_radio_set_selects_first_on_mount():
    """Test that RadioSet selects first option (CSV) on mount."""
    async with SaveScreenTestApp().run_test() as pilot:
        pilot.app.push_screen(SaveScreen(rows=SAMPLE_ROWS, port=8888))
        await pilot.pause()
        radio_set = pilot.app.screen.query_one('#save-format', RadioSet)
        assert radio_set.pressed_index == 0


@pytest.mark.asyncio
async def test_save_screen_format_change_updates_extension():
    """Test that changing RadioSet updates filename extension."""
    async with SaveScreenTestApp().run_test() as pilot:
        pilot.app.push_screen(SaveScreen(rows=SAMPLE_ROWS, port=8888))
        await pilot.pause()
        filename_input = pilot.app.screen.query_one('#save-filename', Input)
        radio_buttons = pilot.app.screen.query(RadioButton)

        # Default should be .csv
        assert filename_input.value.endswith('.csv')

        # Change to Markdown (index 1)
        await pilot.click(radio_buttons[1])
        await pilot.pause()
        assert filename_input.value.endswith('.md')

        # Change to JSON (index 2)
        await pilot.click(radio_buttons[2])
        await pilot.pause()
        assert filename_input.value.endswith('.json')


@pytest.mark.asyncio
async def test_save_screen_notifies_after_save():
    """Test that _do_save calls app.notify with expected message."""
    notify_messages = []

    class NotifySaveTestApp(SaveScreenTestApp):
        def notify(self, message, title=None, **kwargs):
            notify_messages.append((message, title))

    async with NotifySaveTestApp().run_test() as pilot:
        pilot.app.push_screen(SaveScreen(rows=SAMPLE_ROWS, port=8888))
        await pilot.pause()
        await pilot.press('enter')
        await pilot.pause()
        assert len(notify_messages) == 1
        msg, title = notify_messages[0]
        assert title == 'Export'
        assert msg.startswith('Saved as')
        assert 'mmng_port8888_' in msg


@pytest.fixture
def profile_app(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    # Disable only the button debounce animation; keyboard events still run normally.
    monkeypatch.setattr(Button, '_start_active_affect', lambda _button: None)

    async def detect(_screen):
        return '1.4.0', True

    async def forbidden_spawn(*_args, **_kwargs):
        pytest.fail('Profile management spawned a subprocess')

    monkeypatch.setattr(MainScreen, '_detect_mmng_version', detect)
    monkeypatch.setattr(FeedWidget, 'start_streaming', lambda _feed: None)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', forbidden_spawn)

    def make(config=None, ports=(8888,)):
        return Pocsag('multimon-ng', [PortConfig(port) for port in ports], 'US', None, None, sdr_config=config)

    return make


def seed_profiles():
    a = FrequencyProfile('Alpha', SDRConfig(123456789, 12, 0, -12))
    b = FrequencyProfile('Beta', SDRConfig(123456790, 13, 1, 10))
    catalog = ProfileCatalog((a, b), a.name)
    save_profiles(default_profile_path(), catalog)
    return catalog


async def fill_profile_editor(pilot, name='Alpha', frequency='123456789', device='0', gain='12', ppm='-12'):
    for field, value in [('name', name), ('frequency', frequency), ('device', device), ('gain', gain), ('ppm', ppm)]:
        pilot.app.screen.query_one(f'#profile-{field}', Input).value = value
    button = pilot.app.screen.query_one('#profile-save')
    button.focus()
    await pilot.press('enter')
    await pilot.pause()


@pytest.mark.asyncio
async def test_profile_crud_keyboard_production_css_80x24(profile_app):
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press('p')
        await pilot.pause()
        new = app.screen.query_one('#profile-new')
        assert app.screen.region.contains_region(new.region)
        new.focus()
        await pilot.press('enter')
        await pilot.pause()
        await fill_profile_editor(pilot, frequency='invalid')
        assert app.screen.query_one('#profile-frequency', Input).value == 'invalid'
        assert not default_profile_path().exists()
        await fill_profile_editor(pilot)
        assert app.sdr_config is None
        assert load_profiles(default_profile_path()).selected_name is None
        app.screen.query_one('#profile-select').focus()
        await pilot.press('enter')
        await pilot.pause()
        assert app.sdr_config.ppm_correction == -12
        app.screen.query_one('#profile-edit').focus()
        await pilot.press('enter')
        await pilot.pause()
        await fill_profile_editor(pilot, name='Renamed', ppm='25')
        assert app.sdr_config.ppm_correction == 25
        assert app.profile_catalog.selected_name == 'Renamed'
        main = next(screen for screen in app.screen_stack if isinstance(screen, MainScreen))
        status = str(main.query_one('#sdr-profile-status', Label).render())
        assert 'Stored selection: Renamed' in status
        assert 'PPM 25' in status
        app.screen.query_one('#profile-new').focus()
        await pilot.press('enter')
        await pilot.pause()
        await fill_profile_editor(pilot, name='RENAMED')
        assert app.screen.query_one('#profile-name', Input).value == 'RENAMED'
        await pilot.press('escape')
        await pilot.pause()
        app.screen.query_one('#profile-delete').focus()
        await pilot.press('enter')
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()
        assert len(app.profile_catalog.profiles) == 1
        app.screen.query_one('#profile-delete').focus()
        await pilot.press('enter')
        await pilot.pause()
        app.screen.query_one('#profile-confirm-delete').focus()
        await pilot.press('enter')
        await pilot.pause()
        assert app.sdr_config is None
        assert load_profiles(default_profile_path()) == ProfileCatalog()
        status = str(main.query_one('#sdr-profile-status', Label).render())
        assert 'Stored selection: none' in status
        assert 'Not configured' in status
        await pilot.press('escape')
        await pilot.pause()
        assert app.screen.query_one('#sdr-start-8888').disabled


@pytest.mark.asyncio
@pytest.mark.parametrize('cli', [False, True])
async def test_profile_restore_cli_precedence_and_explicit_selection(profile_app, cli):
    catalog = seed_profiles()
    path = default_profile_path()
    original = path.read_bytes()
    config = SDRConfig(222222222, 20) if cli else None
    app = profile_app(config)
    assert app.sdr_config == config
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        assert app.sdr_config == (config if cli else catalog.selected.config)
        assert path.read_bytes() == original
        await pilot.press('p')
        await pilot.pause()
        effective = str(app.screen.query_one('#profile-effective').render())
        assert ('CLI' if cli else 'Alpha') in effective
        assert app.select_profile('Beta')
        assert app.sdr_config == catalog.profiles[1].config
        assert app.profile_source == 'Profile: Beta'


@pytest.mark.asyncio
async def test_profile_binding_is_visible_in_rendered_80x24_footer(profile_app):
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        footer = app.screen.query_one(Footer)
        profiles = next(key for key in footer.query('FooterKey') if key.key == 'p')
        assert profiles.key_display == 'p'
        assert profiles.description == 'Profiles'
        assert profiles.region.width > 0
        assert profiles.region.x >= 0
        assert profiles.region.right <= 80
        rendered = '\n'.join(strip.text for strip in app.screen._compositor.render_strips())  # noqa: SLF001
        assert 'p Profiles' in rendered
        await pilot.press('p')
        await pilot.pause()
        assert isinstance(app.screen, ProfilesScreen)
        await pilot.press('escape')
        await pilot.pause()
        start = app.screen.query_one('#sdr-start-8888', Button)
        stop = app.screen.query_one('#sdr-stop-8888', Button)
        assert app.screen.region.contains_region(start.region)
        assert app.screen.region.contains_region(stop.region)


@pytest.mark.asyncio
async def test_main_status_updates_after_profile_selection_with_all_tuning_fields(profile_app):
    catalog = seed_profiles()
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        status_label = app.screen.query_one('#sdr-profile-status', Label)
        assert status_label.region.height > 0
        assert app.screen.region.contains_region(status_label.region)
        await pilot.press('p')
        await pilot.pause()
        await pilot.press('down')
        await pilot.click(app.screen.query_one('#profile-select', Button))
        await pilot.pause()
        await pilot.press('escape')
        await pilot.pause()

        status = str(app.screen.query_one('#sdr-profile-status', Label).render())
        assert 'Stored selection: Beta' in status
        assert 'Next Start' in status
        assert str(catalog.profiles[1].config.frequency_hz) in status
        assert 'Device 1' in status
        assert 'Gain 13' in status
        assert 'PPM 10' in status
        assert app.screen.query_one(FeedWidget).sdr_session is None
        assert app.screen.region.contains_region(app.screen.query_one('#sdr-start-8888', Button).region)
        assert app.screen.region.contains_region(app.screen.query_one('#sdr-stop-8888', Button).region)


@pytest.mark.asyncio
@pytest.mark.parametrize('cli', [False, True])
async def test_main_status_shows_restored_selection_and_cli_precedence(profile_app, cli):
    catalog = seed_profiles()
    cli_config = SDRConfig(222222222, 20, 2, -3) if cli else None
    app = profile_app(cli_config)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        status = str(app.screen.query_one('#sdr-profile-status', Label).render())
        assert 'Stored selection: Alpha' in status
        assert 'Next Start' in status
        expected = cli_config if cli else catalog.selected.config
        assert str(expected.frequency_hz) in status
        assert f'Device {expected.device_index}' in status
        assert f'Gain {expected.gain_db:g}' in status
        assert f'PPM {expected.ppm_correction}' in status
        assert ('CLI' if cli else 'Profile: Alpha') in status
        assert app.screen.query_one(FeedWidget).sdr_session is None


@pytest.mark.asyncio
async def test_main_status_reports_not_configured_without_sdr_configuration(profile_app):
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        status = str(app.screen.query_one('#sdr-profile-status', Label).render())
        assert 'Not configured' in status
        assert 'Port 8888: stopped' in status
        assert app.screen.query_one(FeedWidget).sdr_session is None


@pytest.mark.asyncio
async def test_main_status_shows_live_receiver_state_separately_from_next_start(profile_app):
    catalog = seed_profiles()
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        feed = app.screen.query_one(FeedWidget)
        running_config = SDRConfig(111111111, 8, 3, 4)
        feed.sdr_session = SimpleNamespace(state='running', config=running_config)
        feed._show_sdr_state('running', None)  # noqa: SLF001
        status = str(app.screen.query_one('#sdr-profile-status', Label).render())
        assert 'Next Start' in status
        assert str(catalog.selected.config.frequency_hz) in status
        assert 'Port 8888: running' in status
        assert str(running_config.frequency_hz) in status
        assert 'Device 3' in status
        assert feed.sdr_session.config == running_config


@pytest.mark.asyncio
async def test_profile_name_input_accepts_plain_p(profile_app):
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press('p')
        await pilot.pause()
        await pilot.click(app.screen.query_one('#profile-new', Button))
        await pilot.pause()
        name = app.screen.query_one('#profile-name', Input)
        name.focus()
        await pilot.press('p')
        await pilot.pause()
        assert name.value == 'p'


@pytest.mark.asyncio
async def test_profile_web_boundary_uses_active_driver_and_never_loads_store(profile_app, monkeypatch):
    seed_profiles()
    monkeypatch.setattr(HeadlessDriver, 'is_web', property(lambda _driver: True))

    def forbidden_load(_path):
        pytest.fail('Web mode accessed the profile store')

    monkeypatch.setattr(pocsag_module, 'load_profiles', forbidden_load)
    app = profile_app(SDRConfig(123456789, 12))
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.sdr_config is None
        status = str(app.screen.query_one('#sdr-profile-status', Label).render())
        assert 'unavailable in web mode' in status
        await pilot.press('p')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert not app.select_profile('Alpha')
        assert not app.delete_profile('Alpha')
        assert not app.save_profile(seed_profile_for_handler())
        feed = app.screen.query_one(FeedWidget)
        await feed.start_sdr()
        assert feed.sdr_session is None


def seed_profile_for_handler():
    return FrequencyProfile('Gamma', SDRConfig(123456789, 12))


@pytest.mark.asyncio
@pytest.mark.parametrize('busy', ['starting', 'running', 'stopping', 'cleanup_failed', 'start_worker', 'stop_worker'])
async def test_profile_handlers_reject_any_busy_feed(profile_app, busy):
    seed_profiles()
    app = profile_app(ports=(8888, 8889))
    async with app.run_test() as pilot:
        await pilot.pause()
        feeds = list(app.screen.query(FeedWidget))
        feed = feeds[1]
        original = app.profile_catalog
        original_config = app.sdr_config
        if busy.endswith('worker'):
            setattr(feed, f'_sdr_{busy}', SimpleNamespace(is_finished=False))
        else:
            feed.sdr_session = SimpleNamespace(state=busy)
        assert not app.select_profile('Beta')
        assert not app.delete_profile('Alpha')
        assert not app.save_profile(seed_profile_for_handler())
        assert app.profile_catalog == original
        assert app.sdr_config == original_config
        feed.sdr_session = None
        feed._sdr_start_worker = None  # noqa: SLF001
        feed._sdr_stop_worker = None  # noqa: SLF001
        assert app.select_profile('Beta')


@pytest.mark.asyncio
@pytest.mark.parametrize('committed', [False, True])
async def test_profile_ui_reconciles_save_failure(profile_app, monkeypatch, committed):
    catalog = seed_profiles()
    app = profile_app()
    save = pocsag_module.save_profiles

    def failing_save(path, updated):
        if committed:
            save(path, updated)
        raise ProfileStoreError('durability uncertain' if committed else 'cannot save', committed=committed)

    async with app.run_test() as pilot:
        await pilot.pause()
        monkeypatch.setattr(pocsag_module, 'save_profiles', failing_save)
        assert app.select_profile('Beta') is committed
        expected = catalog.profiles[1].config if committed else catalog.selected.config
        assert app.sdr_config == expected
        assert app.profile_catalog == load_profiles(default_profile_path())


@pytest.mark.asyncio
async def test_profile_malformed_storage_does_not_break_cli_or_overwrite(profile_app):
    path = default_profile_path()
    path.parent.mkdir(parents=True)
    path.write_text('{broken')
    cli = SDRConfig(123456789, 12)
    app = profile_app(cli)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.sdr_config == cli
        assert not app.save_profile(seed_profile_for_handler())
        assert path.read_text() == '{broken'


@pytest.mark.asyncio
async def test_profile_changed_configuration_cleans_terminal_receiver_before_replacement(profile_app, monkeypatch):
    seed_profiles()
    app = profile_app()
    cleaned = asyncio.Event()
    release = asyncio.Event()
    constructed = []

    class OldReceiver:
        state = 'stopped'
        config = SDRConfig(1, 0)

        async def stop(self):
            cleaned.set()
            await release.wait()

    class NewReceiver:
        def __init__(self, config, _sink, on_state):
            assert release.is_set()
            self.config = config
            self.on_state = on_state
            self.state = 'stopped'
            constructed.append(config)

        async def start(self):
            self.state = 'running'

        async def stop(self):
            self.state = 'stopped'

    async with app.run_test() as pilot:
        await pilot.pause()
        feed = app.screen.query_one(FeedWidget)
        feed.process = SimpleNamespace(stdin=object())
        old = OldReceiver()
        feed.sdr_session = old
        assert app.select_profile('Beta')
        monkeypatch.setattr(pocsag_module, 'RTLFMReceiver', NewReceiver)
        starting = asyncio.create_task(feed.start_sdr())
        try:
            await asyncio.wait_for(cleaned.wait(), timeout=1)
            assert not constructed
            assert feed.sdr_session is old
            release.set()
            await asyncio.wait_for(starting, timeout=1)
            assert constructed == [app.sdr_config]
        finally:
            release.set()
            await starting


@pytest.mark.asyncio
async def test_profile_switch_after_real_receiver_stop_preserves_udp_and_device_ownership(profile_app, monkeypatch):
    catalog = seed_profiles()
    app = profile_app()
    processes = []
    writes = []

    class Process:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stderr = asyncio.StreamReader()
            self.returncode = None
            self.exited = asyncio.Event()

        async def wait(self):
            await self.exited.wait()
            return self.returncode

        def terminate(self):
            self.returncode = -15
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()

        kill = terminate

    class Sink:
        def write(self, data):
            writes.append(data)

        def drain(self):
            pass

    async def spawn(*args, **_kwargs):
        processes.append((args, Process()))
        return processes[-1][1]

    async with app.run_test() as pilot:
        await pilot.pause()
        feed = app.screen.query_one(FeedWidget)
        feed.process = SimpleNamespace(stdin=Sink())
        monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
        handler = UDPHandler(feed, SimpleNamespace(time=lambda: 10), 8888)
        handler.datagram_received(b'udp-before', ('192.0.2.1', 8888))
        await pilot.click('#sdr-start-8888')
        await pilot.pause()
        first = feed.sdr_session
        assert first.state == 'running'
        assert not app.select_profile('Beta')
        competitor = RTLFMReceiver(catalog.selected.config, Sink())
        with pytest.raises(DeviceBusyError):
            await competitor.start()
        handler.datagram_received(b'excluded', ('192.0.2.1', 8888))
        await pilot.click('#sdr-stop-8888')
        await pilot.pause()
        assert first.state == 'stopped'
        assert app.select_profile('Beta')
        assert len(processes) == 1
        handler.datagram_received(b'udp-restored', ('192.0.2.1', 8888))
        assert writes == [b'udp-before', b'udp-restored']
        await pilot.click('#sdr-start-8888')
        await pilot.pause()
        assert feed.sdr_session is not first
        assert feed.sdr_session.config == catalog.profiles[1].config
        assert processes[1][0][-2:] == ('-p', '10')
        await feed.stop_sdr()
        await competitor.start()
        await competitor.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize('unresolved', [False, True])
async def test_profile_replacement_of_terminal_error_is_fenced(profile_app, monkeypatch, unresolved):
    seed_profiles()
    app = profile_app()
    created = []

    class TerminalReceiver:
        config = SDRConfig(1, 0)
        state = 'error'

        async def stop(self):
            if unresolved:
                self.state = 'cleanup_failed'
                message = 'cleanup unresolved'
                raise SdrError(message)

    class Replacement:
        def __init__(self, config, _sink, on_state):
            self.config = config
            self.on_state = on_state
            self.state = 'stopped'
            created.append(config)

        async def start(self):
            self.state = 'running'

        async def stop(self):
            self.state = 'stopped'

    async with app.run_test() as pilot:
        await pilot.pause()
        feed = app.screen.query_one(FeedWidget)
        old = TerminalReceiver()
        feed.sdr_session = old
        feed.process = SimpleNamespace(stdin=object())
        monkeypatch.setattr(pocsag_module, 'RTLFMReceiver', Replacement)
        await feed.start_sdr()
        assert bool(created) is not unresolved
        if unresolved:
            assert feed.sdr_session is old
            assert not app.select_profile('Beta')
            assert app.screen.query_one('#sdr-start-8888').disabled
            assert not app.screen.query_one('#sdr-stop-8888').disabled
            feed.sdr_session = None


@pytest.mark.asyncio
async def test_profile_edit_rename_collision_and_failed_selected_edit_preserve_config(profile_app, monkeypatch):
    seed_profiles()
    app = profile_app()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press('p')
        await pilot.pause()
        original = app.profile_catalog
        config = app.sdr_config
        app.screen.query_one('#profile-edit').focus()
        await pilot.press('enter')
        await pilot.pause()
        ppm_input = app.screen.query_one('#profile-ppm', Input)
        ppm_input.focus()
        await pilot.pause()
        assert app.screen.region.contains_region(ppm_input.region)
        await fill_profile_editor(pilot, name='BETA')
        assert app.profile_catalog == original
        assert app.sdr_config == config

        def fail_save(_path, _catalog):
            message = 'injected save failure'
            raise ProfileStoreError(message)

        monkeypatch.setattr(pocsag_module, 'save_profiles', fail_save)
        await fill_profile_editor(pilot, name='Alpha', ppm='35')
        assert app.screen.query_one('#profile-ppm', Input).value == '35'
        assert app.profile_catalog == original
        assert app.sdr_config == config
        await pilot.press('escape')
        await pilot.pause()
        assert app.profile_catalog == load_profiles(default_profile_path())


@pytest.mark.asyncio
@pytest.mark.parametrize('shutdown', [False, True])
async def test_profile_stop_or_unmount_during_replacement_cleanup_prevents_new_receiver(
    profile_app, monkeypatch, shutdown
):
    seed_profiles()
    app = profile_app()
    entered = asyncio.Event()
    second_stop = asyncio.Event()
    release = asyncio.Event()
    constructions = []
    stop_calls = []

    class OldReceiver:
        config = SDRConfig(1, 0)
        state = 'error'

        async def stop(self):
            stop_calls.append(None)
            self.state = 'stopping'
            entered.set()
            if len(stop_calls) == 2:
                second_stop.set()
            await release.wait()
            self.state = 'stopped'

    class Replacement:
        def __init__(self, config, _sink, on_state):
            self.config = config
            self.on_state = on_state
            self.state = 'stopped'
            constructions.append(config)

        async def start(self):
            self.state = 'running'

        async def stop(self):
            self.state = 'stopped'

    async with app.run_test() as pilot:
        await pilot.pause()
        feed = app.screen.query_one(FeedWidget)
        old = OldReceiver()
        feed.sdr_session = old
        feed.process = SimpleNamespace(stdin=object())
        monkeypatch.setattr(pocsag_module, 'RTLFMReceiver', Replacement)
        starting = asyncio.create_task(feed.start_sdr())
        stopping = None
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            stopping = asyncio.create_task(feed.on_unmount() if shutdown else feed.stop_sdr())
            await asyncio.wait_for(second_stop.wait(), timeout=1)
            release.set()
            await asyncio.wait_for(asyncio.gather(starting, stopping), timeout=1)
            assert constructions == []
            assert feed.sdr_session is old
            assert old.state == 'stopped'
        finally:
            release.set()
            await asyncio.gather(starting, *([stopping] if stopping is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_profile_form_cleanup_failure_keeps_dialog_responsive(profile_app, monkeypatch):
    seed_profiles()
    app = profile_app()
    path = default_profile_path()
    original = path.read_bytes()
    original_unlink = Path.unlink
    retained = []

    def fail_replace(*_args, **_kwargs):
        message = 'injected form replace failure'
        raise OSError(message)

    def fail_unlink(temporary, *args, **kwargs):
        if temporary.parent == path.parent and temporary.name.startswith('.sdr-profiles-'):
            retained.append(temporary)
            message = 'injected form unlink failure'
            raise OSError(message)
        return original_unlink(temporary, *args, **kwargs)

    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.press('p')
        await pilot.pause()
        app.screen.query_one('#profile-edit').focus()
        await pilot.press('enter')
        await pilot.pause()
        original_catalog = app.profile_catalog
        original_config = app.sdr_config
        monkeypatch.setattr('mmng_ui.sdr_profiles.os.replace', fail_replace)
        monkeypatch.setattr(Path, 'unlink', fail_unlink)
        await fill_profile_editor(pilot, name='Alpha', ppm='35')
        error_label = app.screen.query_one('#profile-error')
        error = str(error_label.render())
        assert error_label.region.height > 0
        assert app.screen.region.contains_region(error_label.region)
        visible_error = ' '.join(
            ' '.join(error_label.render_line(y).text for y in range(error_label.size.height)).split()
        )
        assert 'injected form replace failure' in visible_error
        assert 'injected form unlink failure' in visible_error
        assert 'retained temporary file:' in visible_error
        assert 'injected form replace failure' in error
        assert 'injected form unlink failure' in error
        assert 'retained' in error.lower()
        assert len(retained) == 1
        assert retained[0].exists()
        assert str(retained[0]) in error
        assert app.profile_catalog == original_catalog
        assert app.sdr_config == original_config
        assert path.read_bytes() == original
        assert app.screen.query_one('#profile-ppm', Input).value == '35'
        assert 'Profiles saved' not in app.profile_notice
        await pilot.press('tab')
        await pilot.pause()
        assert app.screen.focused is not None
        await pilot.click('#profile-cancel')
        await pilot.pause()
        assert app.screen.query_one('#profile-close')
        await pilot.click('#profile-close')
        await pilot.pause()
        assert isinstance(app.screen, MainScreen)
        assert app.profile_catalog == original_catalog
        assert path.read_bytes() == original


class ProfileLifecycleProcess:
    def __init__(self):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self.exited = asyncio.Event()
        self.allow_reap = asyncio.Event()
        self.allow_reap.set()
        self.reap_started = asyncio.Event()
        self.termination_requested = asyncio.Event()
        self.termination_error = False
        self.reaped = False

    async def wait(self):
        await self.exited.wait()
        self.reap_started.set()
        await self.allow_reap.wait()
        self.reaped = True
        return self.returncode

    def terminate(self):
        self.termination_requested.set()
        if self.termination_error:
            message = 'injected termination failure'
            raise PermissionError(message)

    kill = terminate

    def finish(self, code=-15):
        if self.returncode is None:
            self.returncode = code
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.exited.set()


@pytest.mark.asyncio
async def test_cleanup_failed_stop_retry_via_buttons_restores_udp(profile_app, monkeypatch):
    seed_profiles()
    app = profile_app()
    child = ProfileLifecycleProcess()
    writes = []
    failed = asyncio.Event()
    stopped = asyncio.Event()
    original_show = FeedWidget._show_sdr_state  # noqa: SLF001

    async def spawn(*_args, **_kwargs):
        return child

    def show_state(feed, state, error):
        original_show(feed, state, error)
        if state == 'cleanup_failed':
            failed.set()
        elif state == 'stopped':
            stopped.set()

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(FeedWidget, '_show_sdr_state', show_state)
    async with app.run_test(size=(80, 24)) as pilot:
        feed = app.screen.query_one(FeedWidget)
        feed.process = SimpleNamespace(stdin=SimpleNamespace(write=writes.append, drain=lambda: None))
        handler = UDPHandler(feed, SimpleNamespace(time=lambda: 10), 8888)
        config = app.sdr_config
        try:
            assert await pilot.click('#sdr-start-8888')
            receiver = feed.sdr_session
            assert receiver.state == 'running'
            child.termination_error = True
            assert await pilot.click('#sdr-stop-8888')
            await asyncio.wait_for(failed.wait(), timeout=1)
            assert receiver.state == 'cleanup_failed'
            assert app.screen.query_one('#sdr-start-8888').disabled
            assert not app.screen.query_one('#sdr-stop-8888').disabled
            assert receiver._device_owners[0] is receiver  # noqa: SLF001
            handler.datagram_received(b'failed-cleanup', ('192.0.2.1', 8888))
            assert writes == []
            child.termination_requested.clear()
            child.termination_error = False
            assert await pilot.click('#sdr-stop-8888')
            await asyncio.wait_for(child.termination_requested.wait(), timeout=1)
            worker = feed._sdr_stop_worker  # noqa: SLF001
            assert worker is not None
            assert receiver.state == 'stopping'
            assert receiver._device_owners[0] is receiver  # noqa: SLF001
            handler.datagram_received(b'awaiting-reap', ('192.0.2.1', 8888))
            assert writes == []
            child.finish()
            await asyncio.wait_for(stopped.wait(), timeout=1)
            await asyncio.wait_for(worker.wait(), timeout=1)
            assert worker.error is None
            assert receiver.state == 'stopped'
            assert child.reaped
            assert 0 not in receiver._device_owners  # noqa: SLF001
            assert app.sdr_config == config
            assert not app.screen.query_one('#sdr-start-8888').disabled
            assert app.screen.query_one('#sdr-stop-8888').disabled
            handler.datagram_received(b'restored', ('192.0.2.1', 8888))
            assert writes == [b'restored']
        finally:
            child.termination_error = False
            child.finish()


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['stop', 'unmount', 'stop_then_unmount'])
@pytest.mark.parametrize('cleanup_fails', [False, True])
async def test_profile_replacement_stop_and_real_unmount_events(profile_app, monkeypatch, action, cleanup_fails):
    seed_profiles()
    app = profile_app()
    child = ProfileLifecycleProcess()
    spawned = []
    terminal_error = asyncio.Event()
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    stop_processed = asyncio.Event()
    unmount_entered = asyncio.Event()
    old = None
    gate_cleanup = False
    fail_cleanup = cleanup_fails
    original_show = FeedWidget._show_sdr_state  # noqa: SLF001
    original_terminate = RTLFMReceiver._terminate_process  # noqa: SLF001
    original_stop_worker = FeedWidget._run_sdr_stop  # noqa: SLF001
    original_unmount = FeedWidget.on_unmount

    async def spawn(*args, **_kwargs):
        spawned.append(args)
        assert len(spawned) == 1, 'Replacement must not start after Stop or unmount'
        return child

    def show_state(feed, state, error):
        original_show(feed, state, error)
        if state == 'error':
            terminal_error.set()

    async def terminate(receiver):
        if receiver is old and gate_cleanup:
            cleanup_entered.set()
            await release_cleanup.wait()
            if fail_cleanup:
                message = 'injected replacement cleanup failure'
                raise PermissionError(message)
        await original_terminate(receiver)

    async def stop_worker(feed):
        stop_processed.set()
        await original_stop_worker(feed)

    async def unmount(feed):
        unmount_entered.set()
        await original_unmount(feed)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(FeedWidget, '_show_sdr_state', show_state)
    monkeypatch.setattr(FeedWidget, '_run_sdr_stop', stop_worker)
    monkeypatch.setattr(FeedWidget, 'on_unmount', unmount)
    monkeypatch.setattr(RTLFMReceiver, '_terminate_process', terminate)
    start_click = stop_click = removal = None
    async with app.run_test(size=(80, 24)) as pilot:
        feed = app.screen.query_one(FeedWidget)
        feed.process = SimpleNamespace(stdin=SimpleNamespace(write=lambda _data: None, drain=lambda: None))
        try:
            assert await pilot.click('#sdr-start-8888')
            old = feed.sdr_session
            assert old.state == 'running'
            assert old._device_owners[0] is old  # noqa: SLF001
            child.allow_reap.clear()
            child.finish(code=1)
            await asyncio.wait_for(child.reap_started.wait(), timeout=1)
            assert old._device_owners[0] is old  # noqa: SLF001
            assert not app.profiles_editable
            assert not child.reaped
            child.allow_reap.set()
            await asyncio.wait_for(terminal_error.wait(), timeout=1)
            assert child.reaped
            assert 0 not in old._device_owners  # noqa: SLF001
            await pilot.press('p')
            await pilot.pause()
            app.screen.query_one('#profile-list').focus()
            await pilot.press('down')
            assert await pilot.click('#profile-select')
            assert app.profile_catalog.selected_name == 'Beta'
            assert await pilot.click('#profile-close')
            config = app.sdr_config
            assert config != old.config
            gate_cleanup = True
            start_click = asyncio.create_task(pilot.click('#sdr-start-8888'))
            await asyncio.wait_for(cleanup_entered.wait(), timeout=1)
            start_worker = feed._sdr_start_worker  # noqa: SLF001
            assert start_worker is not None
            assert old.state == 'stopping'
            assert feed.sdr_session is old
            assert len(spawned) == 1
            assert not app.profiles_editable
            stop_handle = None
            if action != 'unmount':
                stop_click = asyncio.create_task(pilot.click('#sdr-stop-8888'))
                await asyncio.wait_for(stop_processed.wait(), timeout=1)
                stop_handle = feed._sdr_stop_worker  # noqa: SLF001
                assert stop_handle is not None
                assert feed._sdr_replacement_request is None  # noqa: SLF001
            if action != 'stop':
                removal = asyncio.ensure_future(feed.remove())
                await asyncio.wait_for(unmount_entered.wait(), timeout=1)
                assert not removal.done()
                assert feed._sdr_replacement_request is None  # noqa: SLF001
            release_cleanup.set()
            await asyncio.wait_for(start_worker.wait(), timeout=1)
            if stop_handle is not None:
                await asyncio.wait_for(stop_handle.wait(), timeout=1)
                assert stop_handle.error is None
            await asyncio.wait_for(
                asyncio.gather(*(task for task in (start_click, stop_click) if task is not None)), timeout=1
            )
            if removal is not None:
                await asyncio.wait_for(removal, timeout=1)
                assert not feed.is_attached
                assert feed not in app.screen.query(FeedWidget)
            assert start_worker.error is None
            assert feed.sdr_session is old
            assert feed._sdr_replacement_request is None  # noqa: SLF001
            assert app.sdr_config == config
            assert len(spawned) == 1
            if cleanup_fails:
                assert old.state == 'cleanup_failed'
                assert 'injected replacement cleanup failure' in old.error
                if action == 'stop':
                    assert 'injected replacement cleanup failure' in app.screen.query_one('#sdr-state-8888').content
                    assert app.screen.query_one('#sdr-start-8888').disabled
                    assert not app.screen.query_one('#sdr-stop-8888').disabled
            else:
                assert old.state == 'stopped'
            assert 0 not in old._device_owners  # noqa: SLF001
        finally:
            fail_cleanup = False
            release_cleanup.set()
            child.allow_reap.set()
            child.finish()
            await asyncio.gather(
                *(task for task in (start_click, stop_click, removal) if task is not None), return_exceptions=True
            )
            if old is not None:
                await asyncio.wait_for(old.stop(), timeout=1)
