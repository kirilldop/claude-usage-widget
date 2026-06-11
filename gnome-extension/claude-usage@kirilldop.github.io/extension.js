// Claude Usage — GNOME Shell panel indicator (GNOME 45+ / ESM).
// Reads ~/.cache/claude-usage-widget/status.json (written by the widget or by
// statusd.py) and shows the 5-hour session % in the top bar, with a dropdown
// for details and buttons to open the full widget.

import GObject from 'gi://GObject';
import St from 'gi://St';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Clutter from 'gi://Clutter';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

const HOME = GLib.get_home_dir();
const STATUS_PATH = HOME + '/.cache/claude-usage-widget/status.json';
const STATUSD = HOME + '/.local/share/claude-usage-widget/statusd.py';
const LAUNCHER = HOME + '/.local/bin/claude-usage-widget';

function pctStr(v) {
    return (v === null || v === undefined) ? '—' : Math.round(v) + '%';
}

function resetStr(iso) {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (isNaN(t)) return '';
    let mins = Math.round((t - Date.now()) / 60000);
    if (mins < 0) mins = 0;
    if (mins < 60) return '  ·  resets in ' + mins + 'm';
    const h = Math.floor(mins / 60), m = mins % 60;
    if (h < 72) return '  ·  resets in ' + h + 'h ' + m + 'm';
    const d = Math.round(h / 24);
    return '  ·  resets in ' + d + 'd';
}

const ClaudeIndicator = GObject.registerClass(
class ClaudeIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.0, 'Claude Usage');

        const box = new St.BoxLayout({style_class: 'panel-status-menu-box'});
        this._dot = new St.Label({
            text: '●', y_align: Clutter.ActorAlign.CENTER,
            style_class: 'cu-dot',
        });
        this._label = new St.Label({
            text: ' —', y_align: Clutter.ActorAlign.CENTER,
            style_class: 'cu-label',
        });
        box.add_child(this._dot);
        box.add_child(this._label);
        this.add_child(box);

        this._i5 = new PopupMenu.PopupMenuItem('5-hour session:  —');
        this._i5.sensitive = false;
        this._i7 = new PopupMenu.PopupMenuItem('Weekly (all models):  —');
        this._i7.sensitive = false;
        this._itok = new PopupMenu.PopupMenuItem('Today:  —');
        this._itok.sensitive = false;
        this.menu.addMenuItem(this._i5);
        this.menu.addMenuItem(this._i7);
        this.menu.addMenuItem(this._itok);
        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        // toggles between opening and hiding depending on the widget state
        // (the widget writes widget_visible into status.json on map/hide)
        this._widgetVisible = false;
        this._toggleItem = new PopupMenu.PopupMenuItem('Open full widget');
        this._toggleItem.connect('activate', () => {
            if (this._widgetVisible) {
                // ask the running app to hide via its exported GAction
                try {
                    Gio.DBus.session.call(
                        'io.github.kirilldop.ClaudeUsage',
                        '/io/github/kirilldop/ClaudeUsage',
                        'org.gtk.Actions', 'Activate',
                        new GLib.Variant('(sava{sv})', ['hide', [], {}]),
                        null, Gio.DBusCallFlags.NONE, -1, null, null);
                } catch (e) { logError(e); }
            } else {
                // spawns or raises (the app is single-instance)
                try { GLib.spawn_command_line_async('"' + LAUNCHER + '"'); }
                catch (e) { logError(e); }
            }
        });
        this.menu.addMenuItem(this._toggleItem);

        const refresh = new PopupMenu.PopupMenuItem('Refresh');
        refresh.connect('activate', () => this.read());
        this.menu.addMenuItem(refresh);

        this.menu.connect('open-state-changed', (m, open) => {
            if (open) this.read();
        });
    }

    read() {
        try {
            const [ok, contents] = GLib.file_get_contents(STATUS_PATH);
            if (!ok) return;
            const s = JSON.parse(new TextDecoder().decode(contents));
            this._update(s);
        } catch (e) {
            // no status yet; leave placeholders
        }
    }

    _update(s) {
        const p5 = pctStr(s.five_hour_pct);
        const p7 = pctStr(s.seven_day_pct);
        // a status file this old means both writers (widget, statusd) are
        // gone — show "stale" instead of pretending the data is live
        const STALE_SEC = 600;
        const age = s.ts ? (Date.now() / 1000 - s.ts) : Infinity;
        const stale = age > STALE_SEC;
        const ok = !stale && s.status === 'ok';
        // offline / rate-limited are soft states: data may be cached but real
        const soft = !stale
            && (s.status === 'offline' || s.status === 'rate_limited');
        // dot color by health, then by session load
        // (thresholds duplicated in widget.py severity() — keep in sync)
        let color = '#e5534b';            // expired / not connected
        if (stale) {
            color = '#9a9a97';            // no fresh data — writers are dead
        } else if (ok) {
            if (s.five_hour_pct === null || s.five_hour_pct === undefined) {
                color = '#9a9a97';        // connected but no data yet
            } else {
                const v = s.five_hour_pct;
                color = v < 70 ? '#5fb98e' : (v < 92 ? '#d97757' : '#e5534b');
            }
        } else if (soft) {
            color = '#d9a23f';
        }
        this._dot.style = 'color: ' + color + ';';
        this._label.text = ' ' + (stale ? '—' : (ok || soft) ? p5 : '!');

        this._i5.label.text = '5-hour session:  ' + p5 + resetStr(s.five_hour_reset);
        this._i7.label.text = 'Weekly (all models):  ' + p7 + resetStr(s.seven_day_reset);
        this._itok.label.text = 'Today:  ' + (s.today_tokens || '—') + ' tokens'
            + (s.burn ? '   ·   ' + s.burn : '');

        this._widgetVisible = !!s.widget_visible;
        this._toggleItem.label.text =
            this._widgetVisible ? 'Hide widget' : 'Open full widget';
    }
});

export default class ClaudeUsageExtension extends Extension {
    enable() {
        // start the headless updater (so the panel works without the GUI open)
        try {
            this._proc = Gio.Subprocess.new(
                ['python3', STATUSD], Gio.SubprocessFlags.NONE);
        } catch (e) {
            logError(e, 'claude-usage: failed to start statusd');
            this._proc = null;
        }

        this._indicator = new ClaudeIndicator();
        Main.panel.addToStatusArea('claude-usage', this._indicator);
        this._indicator.read();

        // watch the status file + a slow periodic refresh (for reset countdowns)
        try {
            this._file = Gio.File.new_for_path(STATUS_PATH);
            this._monitor = this._file.monitor(Gio.FileMonitorFlags.NONE, null);
            this._monitor.connect('changed', () => this._indicator?.read());
        } catch (e) {
            this._monitor = null;
        }
        this._timer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 30, () => {
            this._indicator?.read();
            return GLib.SOURCE_CONTINUE;
        });
    }

    disable() {
        if (this._timer) { GLib.source_remove(this._timer); this._timer = null; }
        if (this._monitor) { this._monitor.cancel(); this._monitor = null; }
        this._file = null;
        if (this._proc) {
            try { this._proc.force_exit(); } catch (e) {}
            this._proc = null;
        }
        this._indicator?.destroy();
        this._indicator = null;
    }
}
