# mmng-ui

A TUI (text user interface) frontend for [multimon-ng](https://github.com/EliasOenal/multimon-ng).

`mmng-ui` will listen on a chosen UDP port for raw streams from software like SDR++, use `multimon-ng` to decode it,
and show you POCSAG/FLEX messages in a wonderful text interface.

You can also use it within a web browser!

<a href="https://www.buymeacoffee.com/lingfish" target="_blank"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-red.png" alt="Buy Me A Coffee" style="height: 60px !important;width: 217px !important;" ></a>


## Table of contents

<!-- TOC -->
* [mmng-ui](#mmng-ui)
  * [Table of contents](#table-of-contents)
  * [Purpose](#purpose)
  * [Installation](#installation)
    * [Normal TUI (text console) mode](#normal-tui-text-console-mode)
    * [Web mode](#web-mode)
  * [How to use it](#how-to-use-it)
    * [SDR frequency profiles](#sdr-frequency-profiles)
    * [JSON detection](#json-detection)
    * [sox conversion mode](#sox-conversion-mode)
    * [Multiple ports and tabs](#multiple-ports-and-tabs)
    * [Capcode database](#capcode-database)
    * [Save tab to file](#save-tab-to-file)
  * [Example screenshot](#example-screenshot)
  * [Supported Python versions](#supported-python-versions)
<!-- TOC -->


## Purpose

Why not?  I know there are other frontends out there, but I haven't seen any for use in a text console.

I also wanted to learn both [Rich](https://github.com/Textualize/rich) and [Textual](https://github.com/Textualize/textual).

See `CHANGELOG.md` for all the latest updates. The links may not be clickable.

## Installation

### Normal TUI (text console) mode

The recommended way to install `mmng-ui` is to use [pipx](https://pipx.pypa.io/stable/).

After getting `pipx` installed, simply run:

```shell
username@host:~$ pipx install mmng-ui
```

Please [don't use pip system-wide](https://docs.python.org/3.11/installing/index.html#installing-into-the-system-python-on-linux).

You can of course also install it using [classic virtualenvs](https://docs.python.org/3/library/venv.html).

### Web mode

Thanks to Textual's [web serving support](https://github.com/Textualize/textual-serve), you can also run `mmng-ui` in a
web browser!

First, install `mmng-ui` with the `web` feature/extra:

```shell
username@host:~$ pipx install 'mmng-ui[web]'
```

Then run `mmng-ui` with the `--serve` option.  Your console will show you the URL to connect to!

By default, it will bind to all interfaces (and both IPv4 and IPv6 if enabled).  See the help screen for other choices.

If you supply either `--serve-host` or `--serve-port`, there's no need to specify `--serve` as well -- it is implied.

## How to use it

See `mmng-ui --help` for CLI options.

Run `mmng-ui`, and you'll be greeted with this screen:

![screenshot](/docs/initial%20screen.png)

Notice in the status pane, it says "Receiver: idle" -- it is now listening for UDP packets sent to the default port
of 8888.

Now go to your favourite SDR application, and send to where `mmng-ui` is running. If possible, make sure you encode and
send in the right sample rate that `multimon-ng` likes, 22050 Hz. It probably helps to send mono too.

If you can't send at 22050 Hz, [click here](#sox-conversion-mode) to read about using `sox` mode.

Alpha POCSAG messages will soon display in the top pane.  The bottom pane will show the raw output from `multimon-ng`,
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

### SDR frequency profiles

In the local console, press `p` to manage named SDR profiles. Use New, Edit,
Select, Delete (with confirmation), and Close; Tab/Shift+Tab and Enter operate
all controls, and Escape cancels the current dialog. The editor scrolls in an
80x24 terminal. Selecting or restoring a profile never starts reception: use
**Start SDR** separately, and **Stop SDR** before changing profiles.

Profiles contain frequency in integer Hz (1-2000000000), a nonnegative RTL-SDR
device index, manual gain in dB (0-50), and integer PPM correction (-1000 to
1000). Device indices are entered manually; no devices are scanned or discovered.
A nonzero PPM correction is passed to `rtl_fm` with `-p`.

Names are trimmed, contain 1-64 characters without controls, and must be unique
without regard to case. Different names may share the same tuning settings.
Creating a profile does not select it. Editing the selected profile updates the
pending configuration after persistence; deleting it clears that configuration.
Profile mutations and selection are blocked while any feed has active reception,
pending lifecycle work, or unresolved cleanup. Stop remains available for cleanup
retry. A different configuration is used only on the next explicit Start.

Explicit `--sdr-frequency-hz` and `--sdr-gain-db` settings, with the existing
optional `--sdr-device-index`, take precedence at launch and are never saved
automatically. Otherwise the saved selection is restored. The profile dialog
shows the stored selection separately from the effective CLI/profile source.
An explicit later Select overrides the CLI configuration for that session.

Profiles are stored at `$XDG_CONFIG_HOME/mmng-ui/sdr-profiles.json`, falling back
to `~/.config/mmng-ui/sdr-profiles.json` when XDG_CONFIG_HOME is unset, empty,
or relative. The versioned JSON contains only profile names, tuning settings and
the selected name; decoded messages and audio are never included. New application
directories use mode 0700 and profile files use mode 0600. Saves use a synced
same-directory temporary file and atomic replacement. A failure before replacement
preserves the previous file. A directory-sync failure after replacement is shown
as uncertain durability, and the displayed catalog is reconciled with the file.
Malformed or unsupported storage is preserved and profile writes are disabled;
repair it outside the app and restart. Symlink and nonregular profile files are
rejected. Use one writer: concurrent instances can overwrite each other's saves.
Configuration ancestor directories must be trusted and not attacker-controlled;
destination checks do not protect against ancestor replacement or redirection.

Profile management and restoration are unavailable in web mode, as determined by
Textual's active runtime driver. Web clients cannot use restored profiles to start
SDR reception. Existing UDP decoding remains available. RTL-SDR hardware and
uConsole acceptance are still pending.

### JSON detection

`mmng-ui` will attempt to auto-detect the output format from `multimon-ng`, and if it looks like JSON, it'll use it.

JSON output was merged into `multimon-ng` [version 1.4.0](https://github.com/EliasOenal/multimon-ng/releases/tag/1.4.0).  Support for older versions will eventually be dropped.

### sox conversion mode

With release 1.3.0, you can use the `--sox-rate` (optionally with `--sox-binary`) CLI arguments and `mmng-ui` will put
an instance of `sox` in the decoding pipeline, before `multimon-ng`. You can use the same format as `sox` accepts,
for example, `48k`, or `32000`.

### Direct RTL-SDR reception

The optional `rtl_fm` backend can receive directly from one RTL-SDR-compatible device and feed 22050 Hz mono PCM into
the existing `multimon-ng` decoder. Configure the frequency in Hz and a manual gain in dB when starting the app:

```shell
mmng-ui --sdr-frequency-hz 123456789 --sdr-gain-db 17.5
```

The device index defaults to `0`; `--sdr-device-index` selects another single device. Use the TUI's **Start SDR** and
**Stop SDR** buttons to control acquisition. SDR is never started automatically. With SoX enabled, use an input rate of
`22050` for SDR acquisition; other SoX input rates remain available for the existing UDP workflow. While SDR is active
on a feed, UDP datagrams continue to arrive but are not forwarded to that feed's decoder, so the two PCM sources are
not mixed.

Offline tests can simulate argument construction and subprocess/PCM lifecycle without an SDR. They do not validate live
radio reception, device compatibility, or decoded signal quality; those require separate hardware testing.

### Multiple ports and tabs

You can listen on multiple UDP ports simultaneously by passing `--port` (or `-p`) more than once:

```shell
mmng-ui --port 8888 --port 8889 --port 8890
```

Each port gets its own tab with an independent status pane, sparkline, and message log.  At launch you can name a tab
using `--port <port>=<name>` or `--port <port>:<name>`:

```shell
mmng-ui --port 8888=Primary --port 8889=Secondary
```

You can also rename the active tab at any time by pressing `r`.

### Capcode database

Pass a JSON or CSV file of known capcodes with `--capcodes` (or `-k`):

```shell
mmng-ui --capcodes /path/to/capcodes.json
```

The file format is an array of objects with `address`, `label`, and optional `icon` and `colour` fields:

```json
[
  { "address": "1234567", "label": "Fire Dept",     "icon": "fire",     "colour": "red" },
  { "address": "7654321", "label": "Ambulance",      "icon": "ambulance" },
  { "address": "1111111", "label": "Local Council" }
]
```

When a message's capcode matches a known entry, its alias is shown in the configured colour with an optional emoji
prefix.  Unmatched capcodes display the raw address as before.

CSV format is also supported:

```csv
address,label,icon,colour
1234567,Fire Dept,fire,red
7654321,Ambulance,ambulance,
```

### Save tab to file

Press `s` while the app is running to open the save dialog.  You can choose a filename and export format -- CSV,
Markdown, or JSON -- to save all messages from the active tab.

## Example screenshot

Here's what a screen full of decodes might look like:

![screenshot](/docs/working%20screen.png)

## Supported Python versions

`mmng-ui` supports Python 3.10 and newer.

## Star history

[![Star History Chart](https://api.star-history.com/chart?repos=lingfish/mmng-ui&type=date&legend=top-left)](https://www.star-history.com/?type=date&repos=lingfish%2Fmmng-ui)
