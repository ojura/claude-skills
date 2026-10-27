#!/usr/bin/env python3
"""Open a tab in a given GNOME Terminal window and run a command in it.

usage: tab-in-window.py <window-id> <cwd> <argv...>

A `gnome-terminal --tab` call puts its tab in whichever terminal window was
last active. This asks gnome-terminal-server over D-Bus instead: CreateInstance
with the window-id option adds the tab to that window, and Exec starts the
command in it. The tab gets this script's environment, so run it under
env-of-claude.sh. `busctl --user tree org.gnome.Terminal` lists the window ids
as /org/gnome/Terminal/window/<id>.
"""
import os
import sys

from gi.repository import Gio, GLib


def nul_terminated(text):
    return text.encode() + b'\0'


def main():
    window_id, cwd, argv = int(sys.argv[1]), sys.argv[2], sys.argv[3:]
    bus = Gio.bus_get_sync(Gio.BusType.SESSION)
    options = {'window-id': GLib.Variant('u', window_id), 'present-window': GLib.Variant('b', True)}
    screen = bus.call_sync('org.gnome.Terminal', '/org/gnome/Terminal/Factory0', 'org.gnome.Terminal.Factory0',
                           'CreateInstance', GLib.Variant('(a{sv})', (options,)), GLib.VariantType('(o)'),
                           Gio.DBusCallFlags.NONE, -1, None).unpack()[0]
    environment = [nul_terminated(f'{k}={v}') for k, v in os.environ.items()]
    exec_options = {'cwd': GLib.Variant('ay', nul_terminated(cwd)), 'environ': GLib.Variant('aay', environment)}
    bus.call_sync('org.gnome.Terminal', screen, 'org.gnome.Terminal.Terminal0', 'Exec',
                  GLib.Variant('(a{sv}aay)', (exec_options, [nul_terminated(a) for a in argv])),
                  None, Gio.DBusCallFlags.NONE, -1, None)
    print(f'started {argv[0]} in {screen}')


if __name__ == '__main__':
    main()
