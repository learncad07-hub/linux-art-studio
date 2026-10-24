import sys
import os
import shutil
import tempfile
import threading
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gtk, Gio, GLib, Gdk

from data.apps import apps_by_category, CATEGORY_ICONS, APPS
from app.helper_client import run_helper_action

APPS_BY_ID = {app.app_id: app for app in APPS}

# Kept in sync with the RPM spec's Version field by hand; there's no build
# system tying the two together yet.
APP_VERSION = "0.1.0"
from app import unreal_state
from app import blender_state
from app import blender_releases
from app import resolve_state
from app import resolve_installer
from app import flatpak_state
from app import log_util

# PENDING_DIALOG_RULE: every Gtk.FileDialog / Adw.AlertDialog / any other
# async or modal dialog must be stored as a persistent attribute (self.x)
# for its entire pending lifetime, from creation until its callback fires,
# never left as a bare local variable. A local-only dialog can be garbage
# collected by Python while the underlying async request is still pending,
# which silently cancels or closes the dialog out from under the user
# (symptom: the dialog flashes open then immediately vanishes). Clear the
# attribute back to None once the callback fires. This bit twice before it
# was written down here, so check this rule before adding any new dialog.

PANEL_CSS = b"""
.panel-categories { background-color: @sidebar_bg_color; }
.panel-apps { background-color: @secondary_sidebar_bg_color; }
.app-badge {
  background-color: @accent_bg_color;
  border-radius: 8px;
  padding: 5px;
}
.terminal-log, .terminal-log text {
  background-color: #1a1a1a;
  color: #d8d8d8;
}
.status-pill-installed {
  background-color: alpha(@success_color, 0.15);
  color: @success_color;
  font-size: 0.8em;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
}
.step-badge {
  background-color: alpha(@accent_bg_color, 0.15);
  color: @accent_color;
  font-size: 0.8em;
  font-weight: 600;
  padding: 2px 10px;
  border-radius: 999px;
}
.result-icon-success {
  background-color: @success_color;
  color: white;
  font-size: 1.6em;
  font-weight: 800;
  border-radius: 999px;
}
.result-icon-failure {
  background-color: @error_color;
  color: white;
  font-size: 1.6em;
  font-weight: 800;
  border-radius: 999px;
}
"""


def make_panel_header(text):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
    label = Gtk.Label(label=text)
    label.add_css_class("heading")
    label.set_halign(Gtk.Align.START)
    label.set_margin_top(12)
    label.set_margin_bottom(10)
    label.set_margin_start(14)
    label.set_margin_end(14)
    box.append(label)
    box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
    return box


def make_app_icon_widget(app, size=32):
    if app.icon_path and os.path.isfile(app.icon_path):
        # The icon file already bakes in its own filled circle backdrop, so
        # it doesn't need (and shouldn't get) the plain-text badge's CSS
        # background too, that would just draw a second, mismatched circle.
        widget = Gtk.Image.new_from_file(app.icon_path)
        widget.set_pixel_size(size)
    else:
        widget = Gtk.Label(label=app.icon_text)
        widget.add_css_class("app-badge")
    widget.set_size_request(size, size)
    widget.set_valign(Gtk.Align.CENTER)
    widget.set_halign(Gtk.Align.CENTER)
    return widget


def make_step_badge(current, total):
    """A small pill showing "Step N of M" on a page that's part of a
    multi-step flow, so it's visually obvious there are more steps
    coming, not just implied by a button. Static per page (set once at
    build time), deliberately separate from the subtitle label next to
    it, which keeps changing with live status text during install."""
    badge = Gtk.Label(label=f"Step {current} of {total}")
    badge.add_css_class("step-badge")
    badge.set_halign(Gtk.Align.CENTER)
    return badge


def install_panel_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(PANEL_CSS)
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
    )


class CategoryListPanel(Gtk.Box):
    """Panel 1 of the 3-panel layout: the list of app categories.

    Supports a compact mode (icon-only rows with a tooltip instead of the
    full icon+label row) via set_compact(), toggled manually from the
    headerbar, independent of window size."""

    EXPANDED_WIDTH = 220
    COMPACT_WIDTH = 60

    def __init__(self, category_names, on_category_selected):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_category_selected = on_category_selected
        self.category_names = category_names
        self._suppress_callback = False
        self.compact = False

        self.header = make_panel_header("Categories")
        self.append(self.header)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_row_selected)
        self._build_rows()

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.listbox)
        self.append(scroller)

    def _build_rows(self):
        row = self.listbox.get_row_at_index(0)
        while row is not None:
            next_row = row.get_next_sibling()
            self.listbox.remove(row)
            row = next_row

        for category_name in self.category_names:
            icon_name = CATEGORY_ICONS.get(category_name, "folder-symbolic")
            if self.compact:
                row = Gtk.ListBoxRow()
                row.set_tooltip_text(category_name)
                icon = Gtk.Image.new_from_icon_name(icon_name)
                icon.set_pixel_size(20)
                icon.set_margin_top(10)
                icon.set_margin_bottom(10)
                icon.set_halign(Gtk.Align.CENTER)
                row.set_child(icon)
            else:
                row = Adw.ActionRow()
                row.set_use_markup(False)
                row.set_title(category_name)
                row.add_prefix(Gtk.Image.new_from_icon_name(icon_name))
            row.category_name = category_name
            self.listbox.append(row)

    def set_compact(self, compact):
        if compact == self.compact:
            return
        selected_row = self.listbox.get_selected_row()
        selected_category = selected_row.category_name if selected_row else None

        self.compact = compact
        self.header.set_visible(not compact)
        self._build_rows()

        if selected_category:
            row = self.listbox.get_row_at_index(0)
            while row is not None:
                if row.category_name == selected_category:
                    self._suppress_callback = True
                    self.listbox.select_row(row)
                    self._suppress_callback = False
                    break
                row = row.get_next_sibling()

    def select_first(self):
        first_row = self.listbox.get_row_at_index(0)
        if first_row is not None:
            self.listbox.select_row(first_row)

    def _on_row_selected(self, _listbox, row):
        if row is not None and not self._suppress_callback:
            self.on_category_selected(row.category_name)


class AppListPanel(Gtk.Box):
    """Panel 2 of the 3-panel layout: the apps within the selected category."""

    def __init__(self, on_app_selected):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_app_selected = on_app_selected
        self.rows_by_app_id = {}

        self.header = make_panel_header("Software")
        self.append(self.header)

        self.listbox = Gtk.ListBox()
        self.listbox.add_css_class("navigation-sidebar")
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.connect("row-selected", self._on_row_selected)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(self.listbox)
        self.append(scroller)

    def show_apps(self, apps):
        row = self.listbox.get_row_at_index(0)
        while row is not None:
            next_row = row.get_next_sibling()
            self.listbox.remove(row)
            row = next_row
        self.rows_by_app_id = {}

        for app in apps:
            row = Adw.ActionRow()
            row.set_use_markup(False)
            row.set_title(app.name)
            row.set_subtitle("")
            row.add_prefix(make_app_icon_widget(app))
            row.app_id = app.app_id
            self.listbox.append(row)
            self.rows_by_app_id[app.app_id] = row

        first_row = self.listbox.get_row_at_index(0)
        if first_row is not None:
            self.listbox.select_row(first_row)

    def set_subtitle(self, app_id, text):
        row = self.rows_by_app_id.get(app_id)
        if row is not None:
            row.set_subtitle(text)

    def _on_row_selected(self, _listbox, row):
        if row is not None:
            self.on_app_selected(row.app_id)


class UnrealPage(Gtk.Box):
    def __init__(self, on_state_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_state_changed = on_state_changed

        self.pending_zip_path = None
        self._file_dialog = None
        self._file_dialog_filters = None
        self._pending_dialog = None
        self.install_in_progress = False
        self.install_cancel_requested = False
        self._install_process = None

        self.inner_stack = Gtk.Stack()
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.inner_stack)
        self.append(self.toast_overlay)

        self.last_installed_version = None

        self._build_list_page()
        self._build_picker_page()
        self._build_progress_page()

        self.inner_stack.set_visible_child_name("list")
        self.refresh()

    # --- list page ---------------------------------------------------

    def _build_list_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        icon_widget = make_app_icon_widget(APPS_BY_ID["ue"], size=56)
        icon_widget.set_halign(Gtk.Align.CENTER)
        page.append(icon_widget)

        title = Gtk.Label(label="Unreal Engine")
        title.add_css_class("title-1")
        page.append(title)

        subtitle = Gtk.Label(
            label="Epic Games' real-time 3D engine for games, film, and visualization. "
                  "Each version installs side by side, nothing gets overwritten"
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        self.versions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.versions_box)

        self.install_button = Gtk.Button(label="Install Unreal Engine")
        self.install_button.add_css_class("suggested-action")
        self.install_button.add_css_class("pill")
        self.install_button.set_halign(Gtk.Align.CENTER)
        self.install_button.set_size_request(260, -1)
        self.install_button.connect("clicked", self._on_install_clicked)
        page.append(self.install_button)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "list")

    def go_to_list(self):
        self.inner_stack.set_visible_child_name("list")

    def refresh(self):
        child = self.versions_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.versions_box.remove(child)
            child = next_child

        versions = unreal_state.list_installed_versions()

        if not versions:
            empty = Gtk.Label(label="No versions installed yet.")
            empty.add_css_class("dim-label")
            self.versions_box.append(empty)
            self.install_button.set_label("Install Unreal Engine")
        else:
            for version, install_dir in versions:
                self.versions_box.append(self._build_version_row(version, install_dir))
            self.install_button.set_label("Install another version")

        self.on_state_changed(len(versions))

    def _build_version_row(self, version, install_dir):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        meta_box.set_hexpand(True)
        version_label = Gtk.Label(label=f"Unreal Engine {version}")
        version_label.set_halign(Gtk.Align.START)
        version_label.add_css_class("heading")
        meta_box.append(version_label)
        path_label = Gtk.Label(label=install_dir)
        path_label.set_halign(Gtk.Align.START)
        path_label.add_css_class("caption")
        path_label.add_css_class("dim-label")
        meta_box.append(path_label)
        header_row.append(meta_box)

        status_pill = Gtk.Label(label="Installed")
        status_pill.add_css_class("status-pill-installed")
        status_pill.set_valign(Gtk.Align.START)
        header_row.append(status_pill)
        box.append(header_row)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        launch_button = Gtk.Button(label="Launch")
        launch_button.add_css_class("pill")
        launch_button.connect("clicked", lambda _b, d=install_dir: self._on_launch_clicked(d))
        button_row.append(launch_button)

        uninstall_button = Gtk.Button(label="Uninstall")
        uninstall_button.add_css_class("destructive-action")
        uninstall_button.add_css_class("pill")
        uninstall_button.connect("clicked", lambda _b, v=version: self._on_uninstall_clicked(v))
        button_row.append(uninstall_button)
        box.append(button_row)

        frame.set_child(box)
        return frame

    def _on_launch_clicked(self, install_dir):
        editor = unreal_state.editor_path(install_dir)
        Gio.AppInfo.create_from_commandline(
            editor, None, Gio.AppInfoCreateFlags.NONE
        ).launch(None, None)

    def _on_uninstall_clicked(self, version):
        # See PENDING_DIALOG_RULE at the top of this file: any async/modal
        # dialog must be held on self for its whole pending lifetime.
        print(f"[alertdialog] opening uninstall confirm for {version}", file=sys.stderr, flush=True)
        dialog = Adw.AlertDialog(
            heading=f"Uninstall Unreal Engine {version}?",
            body=f"This removes /opt/unreal-engine-{version} and its launcher. "
                 f"Other installed versions are not affected.",
        )
        self._pending_dialog = dialog
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dlg, response):
            print(f"[alertdialog] response={response}", file=sys.stderr, flush=True)
            self._pending_dialog = None
            if response == "uninstall":
                self._run_uninstall(version)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _run_uninstall(self, version):
        self.toast_overlay.add_toast(Adw.Toast.new(f"Uninstalling Unreal Engine {version}…"))

        log_path, log_file = log_util.open_log_file(f"uninstall-unreal-{version}")
        log_file.write(f"$ pkexec helper uninstall_unreal {version}\n")
        log_file.flush()

        def on_line(text):
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                self.toast_overlay.add_toast(Adw.Toast.new(f"Unreal Engine {version} uninstalled."))
            else:
                toast = Adw.Toast.new(
                    f"Failed to uninstall Unreal Engine {version} (exit code {exit_code})"
                )
                toast.set_button_label("View log")
                toast.connect("button-clicked", lambda _t: self._open_log_file(log_path))
                self.toast_overlay.add_toast(toast)
            self.refresh()
            return False

        run_helper_action("uninstall_unreal", [version], on_line, on_done)

    # --- install flow, picker page --------------------------------------

    def _on_install_clicked(self, _button):
        if self.install_in_progress:
            self.inner_stack.set_visible_child_name("progress")
            return
        self.pending_zip_path = None
        self.picker_filename_label.set_label("Drop the .zip here, or browse for it")
        self.picker_filename_label.remove_css_class("accent")
        self.picker_install_button.set_sensitive(False)
        self.inner_stack.set_visible_child_name("picker")

    def _build_picker_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        icon_widget = make_app_icon_widget(APPS_BY_ID["ue"], size=48)
        icon_widget.set_halign(Gtk.Align.CENTER)
        page.append(icon_widget)

        page.append(make_step_badge(1, 2))

        title = Gtk.Label(label="Select your downloaded archive")
        title.add_css_class("title-1")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_halign(Gtk.Align.CENTER)
        page.append(title)

        subtitle = Gtk.Label(
            label="Epic requires a manual account login. Download the Linux .zip "
                  "from unrealengine.com/linux first"
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        open_page_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        open_page_row.set_halign(Gtk.Align.CENTER)
        open_page_button = Gtk.Button(label="Open unrealengine.com/linux")
        open_page_button.add_css_class("pill")
        open_page_button.set_halign(Gtk.Align.CENTER)
        open_page_button.connect("clicked", self._on_open_download_page_clicked)
        open_page_row.append(open_page_button)
        self.open_page_status = Gtk.Label(label="")
        self.open_page_status.add_css_class("caption")
        self.open_page_status.add_css_class("dim-label")
        self.open_page_status.set_wrap(True)
        self.open_page_status.set_justify(Gtk.Justification.CENTER)
        self.open_page_status.set_halign(Gtk.Align.CENTER)
        self.open_page_status.set_max_width_chars(40)
        open_page_row.append(self.open_page_status)
        page.append(open_page_row)

        dropzone = Gtk.Frame()
        dropzone.add_css_class("card")
        dropzone_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dropzone_box.set_margin_top(28)
        dropzone_box.set_margin_bottom(28)
        dropzone_box.set_margin_start(16)
        dropzone_box.set_margin_end(16)
        dropzone_box.set_halign(Gtk.Align.CENTER)

        drop_icon = Gtk.Label(label="📦")
        drop_icon.add_css_class("title-1")
        dropzone_box.append(drop_icon)

        self.picker_filename_label = Gtk.Label(label="Drop the .zip here, or browse for it")
        self.picker_filename_label.set_wrap(True)
        self.picker_filename_label.set_justify(Gtk.Justification.CENTER)
        dropzone_box.append(self.picker_filename_label)

        browse_button = Gtk.Button(label="Browse")
        browse_button.add_css_class("pill")
        browse_button.set_halign(Gtk.Align.CENTER)
        browse_button.connect("clicked", self._on_browse_clicked)
        dropzone_box.append(browse_button)

        dropzone.set_child(dropzone_box)
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_file_dropped)
        dropzone.add_controller(drop_target)
        page.append(dropzone)

        location_frame = Gtk.Frame()
        location_frame.add_css_class("card")
        location_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        location_box.set_margin_top(10)
        location_box.set_margin_bottom(10)
        location_box.set_margin_start(14)
        location_box.set_margin_end(14)
        location_box.set_halign(Gtk.Align.CENTER)
        location_label = Gtk.Label(label="Install location")
        location_label.set_halign(Gtk.Align.CENTER)
        location_box.append(location_label)
        location_desc = Gtk.Label(label="/opt/unreal-engine-<version> (version read from the archive)")
        location_desc.set_halign(Gtk.Align.CENTER)
        location_desc.set_justify(Gtk.Justification.CENTER)
        location_desc.set_wrap(True)
        location_desc.add_css_class("caption")
        location_desc.add_css_class("dim-label")
        location_box.append(location_desc)
        location_frame.set_child(location_box)
        page.append(location_frame)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.CENTER)
        back_button = Gtk.Button(label="Back")
        back_button.add_css_class("pill")
        back_button.connect("clicked", lambda _b: self.inner_stack.set_visible_child_name("list"))
        button_row.append(back_button)

        self.picker_install_button = Gtk.Button(label="Install")
        self.picker_install_button.add_css_class("suggested-action")
        self.picker_install_button.add_css_class("pill")
        self.picker_install_button.set_sensitive(False)
        self.picker_install_button.connect("clicked", self._on_picker_install_clicked)
        button_row.append(self.picker_install_button)
        page.append(button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "picker")

    def _on_open_download_page_clicked(self, _button):
        try:
            Gio.AppInfo.launch_default_for_uri("https://www.unrealengine.com/linux", None)
            self.open_page_status.set_label(
                "Opened in your browser, check for a new tab if it didn't come to front"
            )
        except GLib.Error as error:
            self.open_page_status.set_label(f"Couldn't open a browser: {error.message}")

    def _on_browse_clicked(self, _button):
        # Kept on self (dialog AND filters): a purely local Gtk.FileDialog
        # or Gio.ListStore can be garbage collected by Python while the
        # async portal request is still pending, which cancels/closes the
        # dialog out from under the user. See PENDING_DIALOG_RULE.
        print("[filedialog] opening", file=sys.stderr, flush=True)
        dialog = Gtk.FileDialog()
        self._file_dialog = dialog
        filter_zip = Gtk.FileFilter()
        filter_zip.set_name("Unreal Engine archive (.zip)")
        filter_zip.add_pattern("*.zip")
        filter_all = Gtk.FileFilter()
        filter_all.set_name("All files")
        filter_all.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_zip)
        filters.append(filter_all)
        self._file_dialog_filters = filters
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_zip)
        dialog.open(self.get_root(), None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        print("[filedialog] callback fired", file=sys.stderr, flush=True)
        self._file_dialog = None
        self._file_dialog_filters = None
        try:
            file = dialog.open_finish(result)
        except GLib.Error as error:
            print(f"[filedialog] GLib.Error: domain={error.domain} code={error.code} "
                  f"message={error.message}", file=sys.stderr, flush=True)
            if error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                return
            self.picker_filename_label.set_label(f"Couldn't open the file picker: {error.message}")
            return
        print(f"[filedialog] file = {file.get_path() if file else None}", file=sys.stderr, flush=True)
        if self.get_root():
            self.get_root().present()
        if file is None:
            return
        self._set_picked_file(file.get_path())

    def _on_file_dropped(self, _drop_target, value, _x, _y):
        path = value.get_path() if value else None
        if not path or not path.lower().endswith(".zip"):
            return False
        self._set_picked_file(path)
        return True

    def _set_picked_file(self, zip_path):
        self.pending_zip_path = zip_path
        self.picker_filename_label.set_label(os.path.basename(zip_path))
        self.picker_filename_label.add_css_class("accent")
        self.picker_install_button.set_sensitive(True)

    def _on_picker_install_clicked(self, _button):
        if self.pending_zip_path and self.picker_install_button.get_sensitive():
            self.picker_install_button.set_sensitive(False)
            self._start_install(self.pending_zip_path)

    # --- install flow, progress/result page ------------------------------

    def _build_progress_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        self.result_icon = Gtk.Label(label="")
        self.result_icon.set_size_request(56, 56)
        self.result_icon.set_halign(Gtk.Align.CENTER)
        self.result_icon.set_visible(False)
        page.append(self.result_icon)

        self.progress_title = Gtk.Label(label="Installing Unreal Engine")
        self.progress_title.add_css_class("title-1")
        self.progress_title.set_wrap(True)
        self.progress_title.set_justify(Gtk.Justification.CENTER)
        self.progress_title.set_halign(Gtk.Align.CENTER)
        self.progress_title.set_max_width_chars(40)
        page.append(self.progress_title)

        self.progress_subtitle = Gtk.Label(label="This requires administrator privileges and may take a while")
        self.progress_subtitle.add_css_class("dim-label")
        self.progress_subtitle.set_wrap(True)
        self.progress_subtitle.set_justify(Gtk.Justification.CENTER)
        self.progress_subtitle.set_halign(Gtk.Align.CENTER)
        page.append(self.progress_subtitle)

        self.progress_label_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.progress_phase_label = Gtk.Label(label="")
        self.progress_phase_label.add_css_class("caption")
        self.progress_phase_label.add_css_class("dim-label")
        self.progress_phase_label.set_halign(Gtk.Align.START)
        self.progress_phase_label.set_hexpand(True)
        self.progress_label_row.append(self.progress_phase_label)
        self.progress_percent_label = Gtk.Label(label="")
        self.progress_percent_label.add_css_class("caption")
        self.progress_percent_label.add_css_class("dim-label")
        self.progress_percent_label.set_halign(Gtk.Align.END)
        self.progress_label_row.append(self.progress_percent_label)
        page.append(self.progress_label_row)

        self.progress_bar = Gtk.ProgressBar()
        page.append(self.progress_bar)

        log_frame = Gtk.Frame()
        log_frame.add_css_class("card")
        log_frame.set_vexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(10)
        self.log_view.set_bottom_margin(10)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        self.log_view.add_css_class("terminal-log")
        scroller.set_child(self.log_view)
        log_frame.set_child(scroller)

        log_buf = self.log_view.get_buffer()
        self.log_tag_command = log_buf.create_tag("command", foreground="#8a8a8a")
        self.log_tag_error = log_buf.create_tag("error", foreground="#ff6961")
        self.log_tag_success = log_buf.create_tag("success", foreground="#8ff0a4")
        self.log_tag_warn = log_buf.create_tag("warn", foreground="#f8e45c")
        self.log_tag_info = log_buf.create_tag("info", foreground="#7ab8ff")
        self.log_tag_progress = log_buf.create_tag("progress", foreground="#666666")
        page.append(log_frame)

        self.result_button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.result_button_row.set_halign(Gtk.Align.CENTER)
        page.append(self.result_button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "progress")

        self._set_progress_buttons_installing()

    def _clear_result_buttons(self):
        child = self.result_button_row.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.result_button_row.remove(child)
            child = next_child

    def _set_progress_buttons_installing(self):
        self._clear_result_buttons()
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.add_css_class("pill")
        cancel_button.add_css_class("destructive-action")
        cancel_button.connect("clicked", self._on_cancel_install_clicked)
        self.result_button_row.append(cancel_button)
        self.result_icon.set_visible(False)

    def _set_progress_buttons_success(self, log_path):
        self._clear_result_buttons()
        self.result_icon.set_label("✓")
        self.result_icon.remove_css_class("result-icon-failure")
        self.result_icon.add_css_class("result-icon-success")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label="Back to Unreal Engine")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        launch_button = Gtk.Button(label="Launch Unreal Editor")
        launch_button.add_css_class("suggested-action")
        launch_button.add_css_class("pill")
        launch_button.connect("clicked", self._on_launch_just_installed)
        self.result_button_row.append(launch_button)

    def _set_progress_buttons_failure(self, log_path):
        self._clear_result_buttons()
        self.result_icon.set_label("✕")
        self.result_icon.remove_css_class("result-icon-success")
        self.result_icon.add_css_class("result-icon-failure")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label="Back to Unreal Engine")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        try_again_button = Gtk.Button(label="Try again")
        try_again_button.add_css_class("suggested-action")
        try_again_button.add_css_class("pill")
        try_again_button.connect("clicked", self._on_try_again_clicked)
        self.result_button_row.append(try_again_button)

    def _on_try_again_clicked(self, _button):
        # The picked file is still remembered (self.pending_zip_path), the
        # Install button just needs re-enabling: _on_picker_install_clicked
        # disables it on click to prevent a double-submit, and nothing was
        # re-enabling it again on the way back here.
        self.picker_install_button.set_sensitive(self.pending_zip_path is not None)
        self.inner_stack.set_visible_child_name("picker")

    def _open_log_file(self, log_path):
        Gio.AppInfo.launch_default_for_uri(f"file://{log_path}", None)

    def _on_cancel_install_clicked(self, button):
        if not self._install_process or self.install_cancel_requested:
            return
        self.install_cancel_requested = True
        button.set_sensitive(False)
        self.progress_phase_label.set_label("Cancelling…")
        self._append_log("Cancelling install, stopping the process and removing partial files…")

        pid = self._install_process.pid

        def on_line(text):
            self._append_log(text)
            return False

        def on_done(_exit_code):
            return False

        run_helper_action("cancel_install", [str(pid)], on_line, on_done)

    def _on_launch_just_installed(self, _button):
        if self.last_installed_version:
            install_dir = f"{unreal_state.INSTALL_PREFIX}{self.last_installed_version}"
            self._on_launch_clicked(os.path.join("/opt", install_dir))

    def _start_install(self, zip_path):
        if self.install_in_progress:
            print("[install] ignored: an install is already in progress", file=sys.stderr, flush=True)
            self.inner_stack.set_visible_child_name("progress")
            return
        self.install_in_progress = True
        self.install_cancel_requested = False
        self.progress_title.set_label(f"Installing {os.path.basename(zip_path)}")
        self.progress_subtitle.set_label("This requires administrator privileges and may take a while")
        buf = self.log_view.get_buffer()
        buf.set_text("")
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_visible(True)
        self.progress_label_row.set_visible(True)
        self.progress_phase_label.set_label("Starting…")
        self.progress_percent_label.set_label("")
        self._set_progress_buttons_installing()
        self.inner_stack.set_visible_child_name("progress")

        log_path, log_file = log_util.open_log_file("install-unreal")
        self._append_log(f"Log file: {log_path}")
        self._append_log(f"$ pkexec helper install_unreal {zip_path}")
        log_file.write(f"$ pkexec helper install_unreal {zip_path}\n")
        log_file.flush()

        def on_line(text):
            if text.startswith("PROGRESS "):
                try:
                    percent = float(text.split(" ", 1)[1])
                except ValueError:
                    percent = None
                if percent is not None:
                    self.progress_bar.set_fraction(percent / 100)
                    self.progress_phase_label.set_label("Extracting archive…")
                    self.progress_percent_label.set_label(f"{percent:.0f}%")
                log_file.write(text + "\n")
                log_file.flush()
                return False

            if text.startswith("INSTALLED_VERSION="):
                self.last_installed_version = text.split("=", 1)[1]
                log_file.write(text + "\n")
                log_file.flush()
                return False

            if text.startswith("Installing "):
                self.progress_phase_label.set_label("Installing…")
                self.progress_percent_label.set_label("")
            elif text.startswith("Setting ownership"):
                self.progress_phase_label.set_label("Setting ownership…")
                self.progress_percent_label.set_label("")

            self._append_log(text)
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                version = self.last_installed_version
                self.progress_title.set_label("Installation complete")
                if version:
                    self.progress_subtitle.set_label(
                        f"Unreal Engine {version} was installed to /opt/unreal-engine-{version}"
                    )
                else:
                    self.progress_subtitle.set_label("Unreal Engine was installed")
                self.progress_bar.set_visible(False)
                self.progress_label_row.set_visible(False)
                self._set_progress_buttons_success(log_path)
            elif self.install_cancel_requested:
                self.progress_title.set_label("Installation cancelled")
                self.progress_subtitle.set_label("The install was stopped and its partial files removed")
                self.progress_bar.set_visible(False)
                self.progress_label_row.set_visible(False)
                self._set_progress_buttons_failure(log_path)
            else:
                version = self.last_installed_version
                engine_label = f"Unreal Engine {version}" if version else "Unreal Engine"
                self.progress_title.set_label("Installation failed")
                self.progress_subtitle.set_label(f"Something went wrong while installing {engine_label}")
                self.progress_bar.set_visible(False)
                self.progress_label_row.set_visible(False)
                self._set_progress_buttons_failure(log_path)
            self.pending_zip_path = None
            self.install_in_progress = False
            self._install_process = None
            self.refresh()
            return False

        self._install_process = run_helper_action("install_unreal", [zip_path], on_line, on_done)

    def _classify_log_line(self, text):
        lower = text.lower()
        if text.startswith("$") or text.startswith("Log file:"):
            return self.log_tag_command
        if text.startswith("[exited with code "):
            return self.log_tag_success if text.strip() == "[exited with code 0]" else self.log_tag_error
        if "error" in lower or "failed" in lower:
            return self.log_tag_error
        if "warn" in lower:
            return self.log_tag_warn
        if lower.startswith("done.") or "installed" in lower:
            return self.log_tag_success
        if text.startswith("Extracted ") and "files (" in text:
            return self.log_tag_progress
        if text and text[0].isupper() and text.endswith("...") or text.startswith("Ensuring") \
                or text.startswith("Installing") or text.startswith("Setting ownership"):
            return self.log_tag_info
        return None

    def _append_log(self, text):
        buf = self.log_view.get_buffer()
        tag = self._classify_log_line(text)
        end_iter = buf.get_end_iter()
        if tag is not None:
            buf.insert_with_tags(end_iter, text + "\n", tag)
        else:
            buf.insert(end_iter, text + "\n")
        end_mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(end_mark, 0.0, False, 0.0, 0.0)
        return False


class DaVinciPage(Gtk.Box):
    def __init__(self, on_state_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_state_changed = on_state_changed

        self.pending_zip_path = None
        self._file_dialog = None
        self._file_dialog_filters = None
        self._pending_dialog = None
        self.deps_in_progress = False
        self.install_in_progress = False
        self._deps_process = None
        self._install_process = None

        self.inner_stack = Gtk.Stack()
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.inner_stack)
        self.append(self.toast_overlay)

        self._build_list_page()
        self._build_deps_page()
        self._build_picker_page()
        self._build_progress_page()

        self.inner_stack.set_visible_child_name("list")
        self.refresh()

    # --- list page ---------------------------------------------------

    def go_to_list(self):
        self.inner_stack.set_visible_child_name("list")

    def _build_list_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        icon_widget = make_app_icon_widget(APPS_BY_ID["dr"], size=56)
        icon_widget.set_halign(Gtk.Align.CENTER)
        page.append(icon_widget)

        title = Gtk.Label(label="DaVinci Resolve")
        title.add_css_class("title-1")
        page.append(title)

        subtitle = Gtk.Label(
            label="Blackmagic Design's professional video editing, color grading, "
                  "and audio post-production suite"
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        self.status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.status_box)

        self.install_button = Gtk.Button(label="Install dependencies")
        self.install_button.add_css_class("suggested-action")
        self.install_button.add_css_class("pill")
        self.install_button.set_halign(Gtk.Align.CENTER)
        self.install_button.set_size_request(260, -1)
        self.install_button.connect("clicked", self._on_install_deps_clicked)
        page.append(self.install_button)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "list")

    # --- install flow, dependencies page ---------------------------------

    def _on_install_deps_clicked(self, _button):
        if self.deps_in_progress:
            self.inner_stack.set_visible_child_name("deps")
            return
        self._start_deps_install()

    def _build_deps_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        title = Gtk.Label(label="Installing dependencies")
        title.add_css_class("title-1")
        page.append(title)

        subtitle = Gtk.Label(label="Step 1 of 2. Required system packages, this requires "
                                    "administrator privileges")
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        page.append(subtitle)

        log_frame = Gtk.Frame()
        log_frame.add_css_class("card")
        log_frame.set_vexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.deps_log_view = Gtk.TextView()
        self.deps_log_view.set_editable(False)
        self.deps_log_view.set_monospace(True)
        self.deps_log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.deps_log_view.set_top_margin(10)
        self.deps_log_view.set_bottom_margin(10)
        self.deps_log_view.set_left_margin(10)
        self.deps_log_view.set_right_margin(10)
        self.deps_log_view.add_css_class("terminal-log")
        scroller.set_child(self.deps_log_view)
        log_frame.set_child(scroller)

        log_buf = self.deps_log_view.get_buffer()
        self.deps_log_tags = {
            "command": log_buf.create_tag("command", foreground="#8a8a8a"),
            "error": log_buf.create_tag("error", foreground="#ff6961"),
            "success": log_buf.create_tag("success", foreground="#8ff0a4"),
            "warn": log_buf.create_tag("warn", foreground="#f8e45c"),
            "info": log_buf.create_tag("info", foreground="#7ab8ff"),
        }
        page.append(log_frame)

        self.deps_button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.deps_button_row.set_halign(Gtk.Align.CENTER)
        page.append(self.deps_button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "deps")

        self._set_deps_buttons_installing()

    def _clear_deps_buttons(self):
        child = self.deps_button_row.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.deps_button_row.remove(child)
            child = next_child

    def _set_deps_buttons_installing(self):
        self._clear_deps_buttons()
        back_button = Gtk.Button(label="Back")
        back_button.add_css_class("pill")
        back_button.set_sensitive(False)
        self.deps_button_row.append(back_button)

    def _set_deps_buttons_done(self, success):
        self._clear_deps_buttons()
        back_button = Gtk.Button(label="Back")
        back_button.add_css_class("pill")
        back_button.connect("clicked", lambda _b: self.go_to_list())
        self.deps_button_row.append(back_button)

        if success:
            continue_button = Gtk.Button(label="Continue")
            continue_button.add_css_class("suggested-action")
            continue_button.add_css_class("pill")
            continue_button.connect("clicked", self._on_deps_continue_clicked)
            self.deps_button_row.append(continue_button)
        else:
            retry_button = Gtk.Button(label="Try again")
            retry_button.add_css_class("suggested-action")
            retry_button.add_css_class("pill")
            retry_button.connect("clicked", lambda _b: self._start_deps_install())
            self.deps_button_row.append(retry_button)

    def _on_deps_continue_clicked(self, _button):
        self.pending_zip_path = None
        self.picker_filename_label.set_label("Drop the .zip here, or browse for it")
        self.picker_filename_label.remove_css_class("accent")
        self.picker_install_button.set_sensitive(False)
        self.inner_stack.set_visible_child_name("picker")

    def _start_deps_install(self):
        if self.deps_in_progress:
            self.inner_stack.set_visible_child_name("deps")
            return
        self.deps_in_progress = True
        buf = self.deps_log_view.get_buffer()
        buf.set_text("")
        self._set_deps_buttons_installing()
        self.inner_stack.set_visible_child_name("deps")

        log_path, log_file = log_util.open_log_file("install-resolve-deps")
        self._append_log_to(self.deps_log_view, self.deps_log_tags, f"Log file: {log_path}")
        self._append_log_to(self.deps_log_view, self.deps_log_tags, "$ pkexec helper install_resolve_deps")
        log_file.write("$ pkexec helper install_resolve_deps\n")
        log_file.flush()

        def on_line(text):
            self._append_log_to(self.deps_log_view, self.deps_log_tags, text)
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            self.deps_in_progress = False
            self._deps_process = None
            self._set_deps_buttons_done(exit_code == 0)
            return False

        self._deps_process = run_helper_action("install_resolve_deps", [], on_line, on_done)

    def refresh(self):
        child = self.status_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.status_box.remove(child)
            child = next_child

        installed = resolve_state.is_installed()
        self.status_box.append(self._build_resolve_status_row(installed))
        self.status_box.append(self._build_info_row("GPU driver", resolve_state.gpu_status()))
        self.status_box.append(self._build_info_row("SELinux", self._selinux_description()))
        self.status_box.append(self._build_codecs_row(installed))

        self.install_button.set_visible(not installed)
        self.on_state_changed(installed)

    def _selinux_description(self):
        mode = resolve_state.selinux_mode()
        if mode is None:
            return "Not available on this system"
        if mode == "enforcing":
            return "Enforcing. Temporarily set to permissive during the installer step, then restored"
        return mode.capitalize()

    def _build_resolve_status_row(self, installed):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        meta_box.set_hexpand(True)
        name_label = Gtk.Label(label="DaVinci Resolve")
        name_label.set_halign(Gtk.Align.START)
        name_label.add_css_class("heading")
        meta_box.append(name_label)
        path_label = Gtk.Label(label=resolve_state.INSTALL_DIR if installed else "Not installed")
        path_label.set_halign(Gtk.Align.START)
        path_label.add_css_class("caption")
        path_label.add_css_class("dim-label")
        meta_box.append(path_label)
        header_row.append(meta_box)

        if installed:
            status_pill = Gtk.Label(label="Installed")
            status_pill.add_css_class("status-pill-installed")
            status_pill.set_valign(Gtk.Align.START)
            header_row.append(status_pill)
        box.append(header_row)

        if installed:
            button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            launch_button = Gtk.Button(label="Launch")
            launch_button.add_css_class("pill")
            launch_button.connect("clicked", self._on_launch_clicked)
            button_row.append(launch_button)

            uninstall_button = Gtk.Button(label="Uninstall")
            uninstall_button.add_css_class("destructive-action")
            uninstall_button.add_css_class("pill")
            uninstall_button.connect("clicked", self._on_uninstall_clicked)
            button_row.append(uninstall_button)
            box.append(button_row)

        frame.set_child(box)
        return frame

    def _build_info_row(self, label_text, desc_text):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        name_label = Gtk.Label(label=label_text)
        name_label.set_halign(Gtk.Align.START)
        name_label.add_css_class("heading")
        box.append(name_label)

        desc_label = Gtk.Label(label=desc_text)
        desc_label.set_halign(Gtk.Align.START)
        desc_label.set_wrap(True)
        desc_label.add_css_class("caption")
        desc_label.add_css_class("dim-label")
        box.append(desc_label)

        frame.set_child(box)
        return frame

    def _build_codecs_row(self, installed):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        name_label = Gtk.Label(label="Extra codecs (FFmpeg)")
        name_label.set_halign(Gtk.Align.START)
        name_label.add_css_class("heading")
        box.append(name_label)

        desc_label = Gtk.Label(label="Optional, not required to install or run Resolve")
        desc_label.set_halign(Gtk.Align.START)
        desc_label.add_css_class("caption")
        desc_label.add_css_class("dim-label")
        box.append(desc_label)

        codecs_button = Gtk.Button(label="Install extra codecs")
        codecs_button.add_css_class("pill")
        codecs_button.set_halign(Gtk.Align.START)
        codecs_button.set_sensitive(installed)
        codecs_button.connect("clicked", self._on_install_codecs_clicked)
        box.append(codecs_button)

        frame.set_child(box)
        return frame

    def _on_launch_clicked(self, _button):
        Gio.AppInfo.create_from_commandline(
            resolve_state.launcher_path(), None, Gio.AppInfoCreateFlags.NONE
        ).launch(None, None)

    def _on_uninstall_clicked(self, _button):
        # See PENDING_DIALOG_RULE at the top of this file.
        dialog = Adw.AlertDialog(
            heading="Uninstall DaVinci Resolve?",
            body="This runs the vendor uninstaller if present, or removes "
                 "/opt/resolve and its launcher otherwise.",
        )
        self._pending_dialog = dialog
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dlg, response):
            self._pending_dialog = None
            if response == "uninstall":
                self._run_uninstall()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _run_uninstall(self):
        self.toast_overlay.add_toast(Adw.Toast.new("Uninstalling DaVinci Resolve…"))

        log_path, log_file = log_util.open_log_file("uninstall-resolve")

        def finish(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                self.toast_overlay.add_toast(Adw.Toast.new("DaVinci Resolve uninstalled."))
            else:
                toast = Adw.Toast.new(f"Failed to uninstall DaVinci Resolve (exit code {exit_code})")
                toast.set_button_label("View log")
                toast.connect("button-clicked", lambda _t: self._open_log_file(log_path))
                self.toast_overlay.add_toast(toast)
            self.refresh()
            return False

        def run_cleanup():
            # Removes whatever the vendor uninstaller didn't get to (or
            # everything, if there was no vendor uninstaller to run), plus
            # the desktop file and icon. Needs root, so goes through the
            # privileged helper.
            def on_line(text):
                log_file.write(text + "\n")
                log_file.flush()
                return False

            def on_done(exit_code):
                finish(exit_code)
                return False

            run_helper_action("resolve_cleanup", [], on_line, on_done)
            return False

        uninstaller = resolve_state.uninstaller_path()
        if os.path.isfile(uninstaller):
            # Run as the real user, in the real session, not via pkexec:
            # see resolve_installer.py for why that's required whenever
            # Blackmagic's own tooling is launched.
            log_file.write(f"$ {uninstaller}\n")
            log_file.flush()

            def worker():
                def emit(text):
                    log_file.write(text + "\n")
                    log_file.flush()

                resolve_installer.run_with_pty(uninstaller, emit)
                GLib.idle_add(run_cleanup)

            threading.Thread(target=worker, daemon=True).start()
        else:
            log_file.write("No vendor uninstaller found, removing /opt/resolve directly...\n")
            log_file.flush()
            run_cleanup()

    def _on_install_codecs_clicked(self, button):
        button.set_sensitive(False)
        self.toast_overlay.add_toast(Adw.Toast.new("Installing extra codecs…"))

        log_path, log_file = log_util.open_log_file("install-resolve-codecs")
        log_file.write("$ pkexec helper install_resolve_codecs\n")
        log_file.flush()

        def on_line(text):
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            button.set_sensitive(True)
            if exit_code == 0:
                self.toast_overlay.add_toast(Adw.Toast.new("Extra codecs installed."))
            else:
                toast = Adw.Toast.new(f"Failed to install extra codecs (exit code {exit_code})")
                toast.set_button_label("View log")
                toast.connect("button-clicked", lambda _t: self._open_log_file(log_path))
                self.toast_overlay.add_toast(toast)
            return False

        run_helper_action("install_resolve_codecs", [], on_line, on_done)

    # --- install flow, picker page --------------------------------------

    def _build_picker_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        title = Gtk.Label(label="Select your downloaded archive")
        title.add_css_class("title-1")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_halign(Gtk.Align.CENTER)
        page.append(title)

        subtitle = Gtk.Label(
            label="Step 2 of 2. Blackmagic requires a manual account login. Download "
                  "the Linux .zip from blackmagicdesign.com first"
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        open_page_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        open_page_row.set_halign(Gtk.Align.CENTER)
        open_page_button = Gtk.Button(label="Open blackmagicdesign.com")
        open_page_button.add_css_class("pill")
        open_page_button.set_halign(Gtk.Align.CENTER)
        open_page_button.connect("clicked", self._on_open_download_page_clicked)
        open_page_row.append(open_page_button)
        self.open_page_status = Gtk.Label(label="")
        self.open_page_status.add_css_class("caption")
        self.open_page_status.add_css_class("dim-label")
        self.open_page_status.set_wrap(True)
        self.open_page_status.set_justify(Gtk.Justification.CENTER)
        self.open_page_status.set_halign(Gtk.Align.CENTER)
        self.open_page_status.set_max_width_chars(40)
        open_page_row.append(self.open_page_status)
        page.append(open_page_row)

        dropzone = Gtk.Frame()
        dropzone.add_css_class("card")
        dropzone_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dropzone_box.set_margin_top(28)
        dropzone_box.set_margin_bottom(28)
        dropzone_box.set_margin_start(16)
        dropzone_box.set_margin_end(16)
        dropzone_box.set_halign(Gtk.Align.CENTER)

        drop_icon = Gtk.Label(label="📦")
        drop_icon.add_css_class("title-1")
        dropzone_box.append(drop_icon)

        self.picker_filename_label = Gtk.Label(label="Drop the .zip here, or browse for it")
        self.picker_filename_label.set_wrap(True)
        self.picker_filename_label.set_justify(Gtk.Justification.CENTER)
        dropzone_box.append(self.picker_filename_label)

        browse_button = Gtk.Button(label="Browse")
        browse_button.add_css_class("pill")
        browse_button.set_halign(Gtk.Align.CENTER)
        browse_button.connect("clicked", self._on_browse_clicked)
        dropzone_box.append(browse_button)

        dropzone.set_child(dropzone_box)
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_file_dropped)
        dropzone.add_controller(drop_target)
        page.append(dropzone)

        location_frame = Gtk.Frame()
        location_frame.add_css_class("card")
        location_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        location_box.set_margin_top(10)
        location_box.set_margin_bottom(10)
        location_box.set_margin_start(14)
        location_box.set_margin_end(14)
        location_box.set_halign(Gtk.Align.CENTER)
        location_label = Gtk.Label(label="Install location")
        location_label.set_halign(Gtk.Align.CENTER)
        location_box.append(location_label)
        location_desc = Gtk.Label(label="/opt/resolve (fixed path, set by the vendor installer)")
        location_desc.set_halign(Gtk.Align.CENTER)
        location_desc.set_justify(Gtk.Justification.CENTER)
        location_desc.set_wrap(True)
        location_desc.add_css_class("caption")
        location_desc.add_css_class("dim-label")
        location_box.append(location_desc)
        location_frame.set_child(location_box)
        page.append(location_frame)

        heads_up_frame = Gtk.Frame()
        heads_up_frame.add_css_class("card")
        heads_up_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        heads_up_box.set_margin_top(10)
        heads_up_box.set_margin_bottom(10)
        heads_up_box.set_margin_start(14)
        heads_up_box.set_margin_end(14)
        heads_up_box.set_halign(Gtk.Align.CENTER)
        heads_up_label = Gtk.Label(label="Heads up")
        heads_up_label.set_halign(Gtk.Align.CENTER)
        heads_up_box.append(heads_up_label)
        heads_up_desc = Gtk.Label(
            label="The vendor installer opens its own window and runs as your "
                  "normal user, not elevated. Complete it there, this screen "
                  "continues automatically once it closes"
        )
        heads_up_desc.set_halign(Gtk.Align.CENTER)
        heads_up_desc.set_justify(Gtk.Justification.CENTER)
        heads_up_desc.set_wrap(True)
        heads_up_desc.add_css_class("caption")
        heads_up_desc.add_css_class("dim-label")
        heads_up_box.append(heads_up_desc)
        heads_up_frame.set_child(heads_up_box)
        page.append(heads_up_frame)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.CENTER)
        back_button = Gtk.Button(label="Back")
        back_button.add_css_class("pill")
        back_button.connect("clicked", lambda _b: self.inner_stack.set_visible_child_name("deps"))
        button_row.append(back_button)

        self.picker_install_button = Gtk.Button(label="Install")
        self.picker_install_button.add_css_class("suggested-action")
        self.picker_install_button.add_css_class("pill")
        self.picker_install_button.set_sensitive(False)
        self.picker_install_button.connect("clicked", self._on_picker_install_clicked)
        button_row.append(self.picker_install_button)
        page.append(button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "picker")

    def _on_open_download_page_clicked(self, _button):
        try:
            Gio.AppInfo.launch_default_for_uri("https://www.blackmagicdesign.com/products/davinciresolve", None)
            self.open_page_status.set_label(
                "Opened in your browser, check for a new tab if it didn't come to front"
            )
        except GLib.Error as error:
            self.open_page_status.set_label(f"Couldn't open a browser: {error.message}")

    def _on_browse_clicked(self, _button):
        # See PENDING_DIALOG_RULE at the top of this file.
        dialog = Gtk.FileDialog()
        self._file_dialog = dialog
        filter_zip = Gtk.FileFilter()
        filter_zip.set_name("DaVinci Resolve archive (.zip)")
        filter_zip.add_pattern("*.zip")
        filter_all = Gtk.FileFilter()
        filter_all.set_name("All files")
        filter_all.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_zip)
        filters.append(filter_all)
        self._file_dialog_filters = filters
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_zip)
        dialog.open(self.get_root(), None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        self._file_dialog = None
        self._file_dialog_filters = None
        try:
            file = dialog.open_finish(result)
        except GLib.Error as error:
            if error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                return
            self.picker_filename_label.set_label(f"Couldn't open the file picker: {error.message}")
            return
        if self.get_root():
            self.get_root().present()
        if file is None:
            return
        self._set_picked_file(file.get_path())

    def _on_file_dropped(self, _drop_target, value, _x, _y):
        path = value.get_path() if value else None
        if not path or not path.lower().endswith(".zip"):
            return False
        self._set_picked_file(path)
        return True

    def _set_picked_file(self, zip_path):
        self.pending_zip_path = zip_path
        self.picker_filename_label.set_label(os.path.basename(zip_path))
        self.picker_filename_label.add_css_class("accent")
        self.picker_install_button.set_sensitive(True)

    def _on_picker_install_clicked(self, _button):
        if self.pending_zip_path and self.picker_install_button.get_sensitive():
            self.picker_install_button.set_sensitive(False)
            self._start_install(self.pending_zip_path)

    # --- install flow, progress/result page ------------------------------

    def _build_progress_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        self.result_icon = Gtk.Label(label="")
        self.result_icon.set_size_request(56, 56)
        self.result_icon.set_halign(Gtk.Align.CENTER)
        self.result_icon.set_visible(False)
        page.append(self.result_icon)

        self.progress_title = Gtk.Label(label="Installing DaVinci Resolve")
        self.progress_title.add_css_class("title-1")
        self.progress_title.set_wrap(True)
        self.progress_title.set_justify(Gtk.Justification.CENTER)
        self.progress_title.set_halign(Gtk.Align.CENTER)
        self.progress_title.set_max_width_chars(40)
        page.append(self.progress_title)

        self.progress_subtitle = Gtk.Label(label="This requires administrator privileges and may take a while")
        self.progress_subtitle.add_css_class("dim-label")
        self.progress_subtitle.set_wrap(True)
        self.progress_subtitle.set_justify(Gtk.Justification.CENTER)
        self.progress_subtitle.set_halign(Gtk.Align.CENTER)
        page.append(self.progress_subtitle)

        log_frame = Gtk.Frame()
        log_frame.add_css_class("card")
        log_frame.set_vexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(10)
        self.log_view.set_bottom_margin(10)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        self.log_view.add_css_class("terminal-log")
        scroller.set_child(self.log_view)
        log_frame.set_child(scroller)

        log_buf = self.log_view.get_buffer()
        self.log_tags = {
            "command": log_buf.create_tag("command", foreground="#8a8a8a"),
            "error": log_buf.create_tag("error", foreground="#ff6961"),
            "success": log_buf.create_tag("success", foreground="#8ff0a4"),
            "warn": log_buf.create_tag("warn", foreground="#f8e45c"),
            "info": log_buf.create_tag("info", foreground="#7ab8ff"),
        }
        page.append(log_frame)

        self.result_button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.result_button_row.set_halign(Gtk.Align.CENTER)
        page.append(self.result_button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "progress")

        self._set_progress_buttons_installing()

    def _clear_result_buttons(self):
        child = self.result_button_row.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.result_button_row.remove(child)
            child = next_child

    def _set_progress_buttons_installing(self):
        self._clear_result_buttons()
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.add_css_class("pill")
        cancel_button.add_css_class("destructive-action")
        cancel_button.set_sensitive(False)
        self.result_button_row.append(cancel_button)
        self.result_icon.set_visible(False)

    def _set_progress_buttons_success(self, log_path):
        self._clear_result_buttons()
        self.result_icon.set_label("✓")
        self.result_icon.remove_css_class("result-icon-failure")
        self.result_icon.add_css_class("result-icon-success")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label="Back to DaVinci Resolve")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        launch_button = Gtk.Button(label="Launch DaVinci Resolve")
        launch_button.add_css_class("suggested-action")
        launch_button.add_css_class("pill")
        launch_button.connect("clicked", self._on_launch_clicked)
        self.result_button_row.append(launch_button)

    def _set_progress_buttons_failure(self, log_path):
        self._clear_result_buttons()
        self.result_icon.set_label("✕")
        self.result_icon.remove_css_class("result-icon-success")
        self.result_icon.add_css_class("result-icon-failure")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label="Back to DaVinci Resolve")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        try_again_button = Gtk.Button(label="Try again")
        try_again_button.add_css_class("suggested-action")
        try_again_button.add_css_class("pill")
        try_again_button.connect("clicked", self._on_try_again_clicked)
        self.result_button_row.append(try_again_button)

    def _on_try_again_clicked(self, _button):
        # Same fix as UnrealPage: the picked zip is still remembered, only
        # the Install button's disabled-on-click state needs clearing.
        self.picker_install_button.set_sensitive(self.pending_zip_path is not None)
        self.inner_stack.set_visible_child_name("picker")

    def _open_log_file(self, log_path):
        Gio.AppInfo.launch_default_for_uri(f"file://{log_path}", None)

    def _start_install(self, zip_path):
        if self.install_in_progress:
            self.inner_stack.set_visible_child_name("progress")
            return
        self.install_in_progress = True
        self.progress_title.set_label("Installing DaVinci Resolve")
        self.progress_subtitle.set_label("This requires administrator privileges and may take a while")
        buf = self.log_view.get_buffer()
        buf.set_text("")
        self._set_progress_buttons_installing()
        self.inner_stack.set_visible_child_name("progress")

        log_path, log_file = log_util.open_log_file("install-resolve")
        self._append_log(f"Log file: {log_path}")
        log_file.write(f"Log file: {log_path}\n")
        log_file.flush()

        def log(text):
            self._append_log(text)
            log_file.write(text + "\n")
            log_file.flush()

        def finish(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                self.progress_title.set_label("Installation complete")
                self.progress_subtitle.set_label(
                    f"DaVinci Resolve was installed to {resolve_state.INSTALL_DIR}"
                )
                self._set_progress_buttons_success(log_path)
            else:
                self.progress_title.set_label("Installation failed")
                self.progress_subtitle.set_label("Something went wrong while installing DaVinci Resolve")
                self._set_progress_buttons_failure(log_path)
            self.pending_zip_path = None
            self.install_in_progress = False
            self.refresh()

        # The vendor installer itself must run directly, as this process's
        # own user and in this process's own session, not through
        # pkexec/runuser -- see resolve_installer.py for why. Only the
        # SELinux toggle and the post-install fixes below genuinely need
        # root, so those go through the privileged helper, bracketing the
        # unprivileged installer run.
        original_selinux_mode = resolve_state.selinux_mode()
        selinux_was_enforcing = original_selinux_mode == "enforcing"

        def run_post_install_steps(install_exit_code):
            def after_selinux_restored():
                if install_exit_code != 0 or not resolve_state.is_installed():
                    finish(install_exit_code if install_exit_code != 0 else 1)
                    return

                log("Applying post-install compatibility fixes...")

                def on_fix_line(text):
                    log(text)
                    return False

                def on_fix_done(fix_exit_code):
                    finish(fix_exit_code)
                    return False

                run_helper_action("resolve_post_install_fixes", [], on_fix_line, on_fix_done)

            if selinux_was_enforcing:
                def on_restore_line(_text):
                    return False

                def on_restore_done(restore_exit_code):
                    log("SELinux restored to enforcing." if restore_exit_code == 0
                        else "ERROR: could not restore SELinux to enforcing.")
                    after_selinux_restored()
                    return False

                run_helper_action("set_resolve_selinux", ["enforcing"], on_restore_line, on_restore_done)
            else:
                after_selinux_restored()

        def start_installer():
            tmp_dir = tempfile.mkdtemp(prefix="resolve-extract-")

            def worker():
                def emit(text):
                    GLib.idle_add(log, text)

                try:
                    emit("Extracting DaVinci Resolve archive (large, this can take several minutes)...")

                    def on_progress(percent, index, total):
                        emit(f"PROGRESS {percent:.1f}")
                        emit(f"Extracted {index}/{total} files ({percent:.1f}%)")

                    resolve_installer.extract_zip(zip_path, tmp_dir, on_progress)

                    run_path = resolve_installer.find_run_installer(tmp_dir)
                    if run_path is None:
                        emit("ERROR: no .run installer found inside the downloaded archive.")
                        GLib.idle_add(run_post_install_steps, 1)
                        return

                    emit("Launching the DaVinci Resolve installer, running as your user...")
                    exit_code = resolve_installer.run_with_pty(run_path, emit)
                    GLib.idle_add(run_post_install_steps, exit_code)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)

            threading.Thread(target=worker, daemon=True).start()

        if selinux_was_enforcing:
            log("SELinux is enforcing, setting permissive for this step...")

            def on_selinux_line(_text):
                return False

            def on_selinux_done(exit_code):
                log("SELinux set to permissive (temporary)." if exit_code == 0
                    else "ERROR: could not set SELinux to permissive, continuing anyway.")
                start_installer()
                return False

            run_helper_action("set_resolve_selinux", ["permissive"], on_selinux_line, on_selinux_done)
        else:
            start_installer()

    def _classify_log_line(self, text, tags):
        lower = text.lower()
        if text.startswith("$") or text.startswith("Log file:"):
            return tags["command"]
        if text.startswith("[exited with code "):
            return tags["success"] if text.strip() == "[exited with code 0]" else tags["error"]
        if "error" in lower or "failed" in lower:
            return tags["error"]
        if "warn" in lower or "waiting" in lower:
            return tags["warn"]
        if lower.startswith("done.") or "installed" in lower or "restored" in lower:
            return tags["success"]
        if text and text[0].isupper() and (text.endswith("...") or text.startswith("Installing")
                                            or text.startswith("Launching") or text.startswith("Applying")):
            return tags["info"]
        return None

    def _append_log_to(self, log_view, tags, text):
        buf = log_view.get_buffer()
        tag = self._classify_log_line(text, tags)
        end_iter = buf.get_end_iter()
        if tag is not None:
            buf.insert_with_tags(end_iter, text + "\n", tag)
        else:
            buf.insert(end_iter, text + "\n")
        end_mark = buf.create_mark(None, buf.get_end_iter(), False)
        log_view.scroll_to_mark(end_mark, 0.0, False, 0.0, 0.0)
        return False

    def _append_log(self, text):
        return self._append_log_to(self.log_view, self.log_tags, text)


class BlenderPage(Gtk.Box):
    def __init__(self, on_state_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.on_state_changed = on_state_changed

        self.pending_zip_path = None
        self.pending_build = None
        self._file_dialog = None
        self._file_dialog_filters = None
        self._pending_dialog = None
        self.install_in_progress = False
        self.install_cancel_requested = False
        self._install_process = None

        self.inner_stack = Gtk.Stack()
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.inner_stack)
        self.append(self.toast_overlay)

        self.last_installed_version = None

        self._build_list_page()
        self._build_picker_page()
        self._build_progress_page()

        self.inner_stack.set_visible_child_name("list")
        self.refresh()

    # --- list page ---------------------------------------------------

    def _build_list_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        icon_widget = make_app_icon_widget(APPS_BY_ID["blender"], size=56)
        icon_widget.set_halign(Gtk.Align.CENTER)
        page.append(icon_widget)

        title = Gtk.Label(label="Blender")
        title.add_css_class("title-1")
        page.append(title)

        subtitle = Gtk.Label(
            label="Free, open-source 3D creation suite for modeling, animation, and "
                  "rendering. Each version installs side by side, nothing gets overwritten"
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        self.versions_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.versions_box)

        self.install_button = Gtk.Button(label="Install Blender")
        self.install_button.add_css_class("suggested-action")
        self.install_button.add_css_class("pill")
        self.install_button.set_halign(Gtk.Align.CENTER)
        self.install_button.set_size_request(260, -1)
        self.install_button.connect("clicked", self._on_install_clicked)
        page.append(self.install_button)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "list")

    def go_to_list(self):
        self.inner_stack.set_visible_child_name("list")

    def refresh(self):
        child = self.versions_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.versions_box.remove(child)
            child = next_child

        versions = blender_state.list_installed_versions()

        if not versions:
            empty = Gtk.Label(label="No versions installed yet.")
            empty.add_css_class("dim-label")
            self.versions_box.append(empty)
            self.install_button.set_label("Install Blender")
        else:
            for version, install_dir in versions:
                self.versions_box.append(self._build_version_row(version, install_dir))
            self.install_button.set_label("Install another version")

        self.on_state_changed(len(versions))

    def _build_version_row(self, version, install_dir):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        meta_box.set_hexpand(True)
        version_label = Gtk.Label(label=f"Blender {version}")
        version_label.set_halign(Gtk.Align.START)
        version_label.add_css_class("heading")
        meta_box.append(version_label)
        path_label = Gtk.Label(label=install_dir)
        path_label.set_halign(Gtk.Align.START)
        path_label.add_css_class("caption")
        path_label.add_css_class("dim-label")
        meta_box.append(path_label)
        header_row.append(meta_box)

        status_pill = Gtk.Label(label="Installed")
        status_pill.add_css_class("status-pill-installed")
        status_pill.set_valign(Gtk.Align.START)
        header_row.append(status_pill)
        box.append(header_row)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        launch_button = Gtk.Button(label="Launch")
        launch_button.add_css_class("pill")
        launch_button.connect("clicked", lambda _b, d=install_dir: self._on_launch_clicked(d))
        button_row.append(launch_button)

        uninstall_button = Gtk.Button(label="Uninstall")
        uninstall_button.add_css_class("destructive-action")
        uninstall_button.add_css_class("pill")
        uninstall_button.connect("clicked", lambda _b, v=version: self._on_uninstall_clicked(v))
        button_row.append(uninstall_button)
        box.append(button_row)

        frame.set_child(box)
        return frame

    def _on_launch_clicked(self, install_dir):
        binary = blender_state.binary_path(install_dir)
        Gio.AppInfo.create_from_commandline(
            binary, None, Gio.AppInfoCreateFlags.NONE
        ).launch(None, None)

    def _on_uninstall_clicked(self, version):
        # See PENDING_DIALOG_RULE at the top of this file.
        dialog = Adw.AlertDialog(
            heading=f"Uninstall Blender {version}?",
            body=f"This removes /opt/blender-{version} and its launcher. "
                 f"Other installed versions are not affected.",
        )
        self._pending_dialog = dialog
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dlg, response):
            self._pending_dialog = None
            if response == "uninstall":
                self._run_uninstall(version)

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _run_uninstall(self, version):
        self.toast_overlay.add_toast(Adw.Toast.new(f"Uninstalling Blender {version}…"))

        log_path, log_file = log_util.open_log_file(f"uninstall-blender-{version}")
        log_file.write(f"$ pkexec helper uninstall_blender {version}\n")
        log_file.flush()

        def on_line(text):
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                self.toast_overlay.add_toast(Adw.Toast.new(f"Blender {version} uninstalled."))
            else:
                toast = Adw.Toast.new(
                    f"Failed to uninstall Blender {version} (exit code {exit_code})"
                )
                toast.set_button_label("View log")
                toast.connect("button-clicked", lambda _t: self._open_log_file(log_path))
                self.toast_overlay.add_toast(toast)
            self.refresh()
            return False

        run_helper_action("uninstall_blender", [version], on_line, on_done)

    # --- install flow, picker page --------------------------------------

    def _on_install_clicked(self, _button):
        if self.install_in_progress:
            self.inner_stack.set_visible_child_name("progress")
            return
        self.pending_zip_path = None
        self.pending_build = None
        self.picker_filename_label.set_label("Drop the archive here, or browse for it")
        self.picker_filename_label.remove_css_class("accent")
        self.picker_install_button.set_sensitive(False)
        self.inner_stack.set_visible_child_name("picker")
        self._refresh_builds_list()

    def _build_picker_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        title = Gtk.Label(label="Choose a Blender version")
        title.add_css_class("title-1")
        title.set_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        title.set_halign(Gtk.Align.CENTER)
        page.append(title)

        subtitle = Gtk.Label(
            label="Pick a version to download directly, or browse for an archive you already have"
        )
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        self.builds_status_label = Gtk.Label(label="Loading available versions…")
        self.builds_status_label.add_css_class("caption")
        self.builds_status_label.add_css_class("dim-label")
        page.append(self.builds_status_label)

        self.builds_variant_filters = {}
        variant_filter_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        variant_filter_row.set_halign(Gtk.Align.CENTER)
        for variant, label in (
            ("stable", "Stable"), ("candidate", "RC"), ("beta", "Beta"),
            ("alpha", "Alpha"), ("archive", "Older"),
        ):
            toggle = Gtk.ToggleButton(label=label)
            toggle.set_active(variant != "archive")  # current builds on by default, full history opt-in
            toggle.connect("toggled", self._on_builds_variant_filter_toggled)
            self.builds_variant_filters[variant] = toggle
            variant_filter_row.append(toggle)
        page.append(variant_filter_row)

        self.builds_lts_only_toggle = Gtk.ToggleButton(label="LTS only 🛡")
        self.builds_lts_only_toggle.set_halign(Gtk.Align.CENTER)
        self.builds_lts_only_toggle.set_margin_bottom(4)
        self.builds_lts_only_toggle.connect("toggled", self._on_builds_variant_filter_toggled)
        page.append(self.builds_lts_only_toggle)

        self.builds_search_entry = Gtk.SearchEntry()
        self.builds_search_entry.set_placeholder_text("Filter by version (e.g. 4.4)…")
        self.builds_search_entry.connect("search-changed", self._on_builds_search_changed)
        page.append(self.builds_search_entry)

        builds_frame = Gtk.Frame()
        builds_frame.add_css_class("card")
        builds_scroller = Gtk.ScrolledWindow()
        builds_scroller.set_min_content_height(150)
        builds_scroller.set_max_content_height(220)
        self.builds_listbox = Gtk.ListBox()
        self.builds_listbox.add_css_class("boxed-list")
        self.builds_listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.builds_listbox.connect("row-selected", self._on_build_row_selected)
        self.builds_listbox.set_filter_func(self._filter_build_row)
        builds_scroller.set_child(self.builds_listbox)
        builds_frame.set_child(builds_scroller)
        page.append(builds_frame)

        # Collapsed by default: the version list above covers the normal
        # case (Blender isn't gated behind a login the way Unreal Engine
        # and Resolve are, so there's no real need to make manually
        # downloading a file the default path), this stays available as
        # a fallback for offline use, a build that isn't in the list, or
        # if the live version list ever fails to load.
        self.advanced_expander = Gtk.Expander(label="Advanced: browse for an archive you already have")
        self.advanced_expander.set_expanded(False)
        advanced_expander = self.advanced_expander

        dropzone = Gtk.Frame()
        dropzone.add_css_class("card")
        dropzone.set_margin_top(10)
        dropzone_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        dropzone_box.set_margin_top(20)
        dropzone_box.set_margin_bottom(20)
        dropzone_box.set_margin_start(16)
        dropzone_box.set_margin_end(16)
        dropzone_box.set_halign(Gtk.Align.CENTER)

        drop_icon = Gtk.Label(label="📦")
        drop_icon.add_css_class("title-1")
        dropzone_box.append(drop_icon)

        self.picker_filename_label = Gtk.Label(label="Drop the archive here, or browse for it")
        self.picker_filename_label.set_wrap(True)
        self.picker_filename_label.set_justify(Gtk.Justification.CENTER)
        dropzone_box.append(self.picker_filename_label)

        browse_button = Gtk.Button(label="Browse")
        browse_button.add_css_class("pill")
        browse_button.set_halign(Gtk.Align.CENTER)
        browse_button.connect("clicked", self._on_browse_clicked)
        dropzone_box.append(browse_button)

        dropzone.set_child(dropzone_box)
        drop_target = Gtk.DropTarget.new(Gio.File, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_file_dropped)
        dropzone.add_controller(drop_target)

        advanced_expander.set_child(dropzone)
        page.append(advanced_expander)

        location_frame = Gtk.Frame()
        location_frame.add_css_class("card")
        location_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        location_box.set_margin_top(10)
        location_box.set_margin_bottom(10)
        location_box.set_margin_start(14)
        location_box.set_margin_end(14)
        location_box.set_halign(Gtk.Align.CENTER)
        location_label = Gtk.Label(label="Install location")
        location_label.set_halign(Gtk.Align.CENTER)
        location_box.append(location_label)
        location_desc = Gtk.Label(label="/opt/blender-<version> (version read from the archive)")
        location_desc.set_halign(Gtk.Align.CENTER)
        location_desc.set_justify(Gtk.Justification.CENTER)
        location_desc.set_wrap(True)
        location_desc.add_css_class("caption")
        location_desc.add_css_class("dim-label")
        location_box.append(location_desc)
        location_frame.set_child(location_box)
        page.append(location_frame)

        button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        button_row.set_halign(Gtk.Align.CENTER)
        back_button = Gtk.Button(label="Back")
        back_button.add_css_class("pill")
        back_button.connect("clicked", lambda _b: self.inner_stack.set_visible_child_name("list"))
        button_row.append(back_button)

        self.picker_install_button = Gtk.Button(label="Install")
        self.picker_install_button.add_css_class("suggested-action")
        self.picker_install_button.add_css_class("pill")
        self.picker_install_button.set_sensitive(False)
        self.picker_install_button.connect("clicked", self._on_picker_install_clicked)
        button_row.append(self.picker_install_button)
        page.append(button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)

        # This page has more content than Unreal Engine's or Resolve's
        # pickers (filter chips, search, version list, and the advanced
        # archive section all stack up), so unlike those it needs to
        # actually scroll at the default window size: without this the
        # Install button at the bottom becomes unreachable once the
        # advanced section is expanded, confirmed by actually hitting it.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_vexpand(True)
        scroller.set_child(clamp)
        self.inner_stack.add_named(scroller, "picker")

    def _refresh_builds_list(self):
        row = self.builds_listbox.get_row_at_index(0)
        while row is not None:
            next_row = row.get_next_sibling()
            self.builds_listbox.remove(row)
            row = next_row
        self.builds_search_entry.set_text("")
        self.builds_status_label.set_label("Loading available versions…")

        # The current-branches list (Stable/RC/Beta/Alpha) is fast
        # (~1s), so it's shown as soon as it arrives rather than waiting
        # on the full historical archive too, which takes a few seconds
        # longer (confirmed: ~3s for the whole history back to 1.0).
        def worker_current():
            try:
                builds = blender_releases.fetch_linux_builds()
            except Exception as exc:
                GLib.idle_add(self._on_builds_fetch_failed, str(exc))
                return
            GLib.idle_add(self._on_builds_fetched, builds, False)

        def worker_archive():
            try:
                builds = blender_releases.fetch_archive_builds()
            except Exception:
                return  # the current-builds list on its own is still useful
            GLib.idle_add(self._on_builds_fetched, builds, True)

        threading.Thread(target=worker_current, daemon=True).start()
        threading.Thread(target=worker_archive, daemon=True).start()

    def _on_builds_fetch_failed(self, message):
        self.builds_status_label.set_label(f"Couldn't load the version list: {message}")
        self.advanced_expander.set_expanded(True)
        return False

    def _on_builds_fetched(self, builds, is_archive):
        if not builds and not is_archive:
            self.builds_status_label.set_label("No versions found, try browsing for a file instead.")
            self.advanced_expander.set_expanded(True)
            return False
        if not is_archive:
            self.builds_status_label.set_label("Pick a version:")
        for build in builds:
            row = Adw.ActionRow()
            title = f"Blender {build['version']}"
            if build.get("is_lts"):
                title += "  🛡 LTS"
            row.set_title(title)
            row.set_subtitle(build["variant_label"])
            row.build = build
            self.builds_listbox.append(row)
        return False

    def _on_builds_variant_filter_toggled(self, _button):
        self.builds_listbox.invalidate_filter()

    def _on_builds_search_changed(self, _entry):
        self.builds_listbox.invalidate_filter()

    def _filter_build_row(self, row):
        build = row.build
        toggle = self.builds_variant_filters.get(build["variant"])
        if toggle is not None and not toggle.get_active():
            return False
        if self.builds_lts_only_toggle.get_active() and not build.get("is_lts"):
            return False

        query = self.builds_search_entry.get_text().strip().lower()
        if not query:
            return True
        lts_tag = "lts" if build.get("is_lts") else ""
        haystack = f"{build['version']} {build['variant']} {build['variant_label']} {lts_tag}".lower()
        return query in haystack

    def _on_build_row_selected(self, _listbox, row):
        if row is None:
            return
        self.pending_build = row.build
        self.pending_zip_path = None
        self.picker_filename_label.set_label("Drop the archive here, or browse for it")
        self.picker_filename_label.remove_css_class("accent")
        self.picker_install_button.set_sensitive(True)

    def _on_browse_clicked(self, _button):
        # See PENDING_DIALOG_RULE at the top of this file.
        dialog = Gtk.FileDialog()
        self._file_dialog = dialog
        filter_tar = Gtk.FileFilter()
        filter_tar.set_name("Blender archive (.tar.xz, .tar.bz2, .tar.gz)")
        filter_tar.add_pattern("*.tar.xz")
        filter_tar.add_pattern("*.tar.bz2")
        filter_tar.add_pattern("*.tar.gz")
        filter_all = Gtk.FileFilter()
        filter_all.set_name("All files")
        filter_all.add_pattern("*")
        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_tar)
        filters.append(filter_all)
        self._file_dialog_filters = filters
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_tar)
        dialog.open(self.get_root(), None, self._on_file_chosen)

    def _on_file_chosen(self, dialog, result):
        self._file_dialog = None
        self._file_dialog_filters = None
        try:
            file = dialog.open_finish(result)
        except GLib.Error as error:
            if error.matches(Gtk.DialogError.quark(), Gtk.DialogError.DISMISSED):
                return
            self.picker_filename_label.set_label(f"Couldn't open the file picker: {error.message}")
            return
        if self.get_root():
            self.get_root().present()
        if file is None:
            return
        self._set_picked_file(file.get_path())

    def _on_file_dropped(self, _drop_target, value, _x, _y):
        path = value.get_path() if value else None
        if not path or not path.lower().endswith((".tar.xz", ".tar.bz2", ".tar.gz")):
            return False
        self._set_picked_file(path)
        return True

    def _set_picked_file(self, zip_path):
        self.pending_zip_path = zip_path
        self.pending_build = None
        self.builds_listbox.unselect_all()
        self.picker_filename_label.set_label(os.path.basename(zip_path))
        self.picker_filename_label.add_css_class("accent")
        self.picker_install_button.set_sensitive(True)

    def _on_picker_install_clicked(self, _button):
        if not self.picker_install_button.get_sensitive():
            return
        if self.pending_build:
            self.picker_install_button.set_sensitive(False)
            self._start_download(self.pending_build)
        elif self.pending_zip_path:
            self.picker_install_button.set_sensitive(False)
            self._start_install(self.pending_zip_path)

    # --- install flow, progress/result page ------------------------------

    def _build_progress_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        self.result_icon = Gtk.Label(label="")
        self.result_icon.set_size_request(56, 56)
        self.result_icon.set_halign(Gtk.Align.CENTER)
        self.result_icon.set_visible(False)
        page.append(self.result_icon)

        self.progress_title = Gtk.Label(label="Installing Blender")
        self.progress_title.add_css_class("title-1")
        self.progress_title.set_wrap(True)
        self.progress_title.set_justify(Gtk.Justification.CENTER)
        self.progress_title.set_halign(Gtk.Align.CENTER)
        self.progress_title.set_max_width_chars(40)
        page.append(self.progress_title)

        self.progress_subtitle = Gtk.Label(label="This requires administrator privileges and may take a while")
        self.progress_subtitle.add_css_class("dim-label")
        self.progress_subtitle.set_wrap(True)
        self.progress_subtitle.set_justify(Gtk.Justification.CENTER)
        self.progress_subtitle.set_halign(Gtk.Align.CENTER)
        page.append(self.progress_subtitle)

        self.progress_label_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        self.progress_phase_label = Gtk.Label(label="")
        self.progress_phase_label.add_css_class("caption")
        self.progress_phase_label.add_css_class("dim-label")
        self.progress_phase_label.set_halign(Gtk.Align.START)
        self.progress_phase_label.set_hexpand(True)
        self.progress_label_row.append(self.progress_phase_label)
        self.progress_percent_label = Gtk.Label(label="")
        self.progress_percent_label.add_css_class("caption")
        self.progress_percent_label.add_css_class("dim-label")
        self.progress_percent_label.set_halign(Gtk.Align.END)
        self.progress_label_row.append(self.progress_percent_label)
        page.append(self.progress_label_row)

        self.progress_bar = Gtk.ProgressBar()
        page.append(self.progress_bar)

        log_frame = Gtk.Frame()
        log_frame.add_css_class("card")
        log_frame.set_vexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(10)
        self.log_view.set_bottom_margin(10)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        self.log_view.add_css_class("terminal-log")
        scroller.set_child(self.log_view)
        log_frame.set_child(scroller)

        log_buf = self.log_view.get_buffer()
        self.log_tag_command = log_buf.create_tag("command", foreground="#8a8a8a")
        self.log_tag_error = log_buf.create_tag("error", foreground="#ff6961")
        self.log_tag_success = log_buf.create_tag("success", foreground="#8ff0a4")
        self.log_tag_warn = log_buf.create_tag("warn", foreground="#f8e45c")
        self.log_tag_info = log_buf.create_tag("info", foreground="#7ab8ff")
        self.log_tag_progress = log_buf.create_tag("progress", foreground="#666666")
        page.append(log_frame)

        self.result_button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.result_button_row.set_halign(Gtk.Align.CENTER)
        page.append(self.result_button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "progress")

        self._set_progress_buttons_installing()

    def _clear_result_buttons(self):
        child = self.result_button_row.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.result_button_row.remove(child)
            child = next_child

    def _set_progress_buttons_installing(self):
        self._clear_result_buttons()
        cancel_button = Gtk.Button(label="Cancel")
        cancel_button.add_css_class("pill")
        cancel_button.add_css_class("destructive-action")
        cancel_button.connect("clicked", self._on_cancel_install_clicked)
        self.result_button_row.append(cancel_button)
        self.result_icon.set_visible(False)

    def _set_progress_buttons_success(self, log_path):
        self._clear_result_buttons()
        self.result_icon.set_label("✓")
        self.result_icon.remove_css_class("result-icon-failure")
        self.result_icon.add_css_class("result-icon-success")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label="Back to Blender")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        launch_button = Gtk.Button(label="Launch Blender")
        launch_button.add_css_class("suggested-action")
        launch_button.add_css_class("pill")
        launch_button.connect("clicked", self._on_launch_just_installed)
        self.result_button_row.append(launch_button)

    def _set_progress_buttons_failure(self, log_path):
        self._clear_result_buttons()
        self.result_icon.set_label("✕")
        self.result_icon.remove_css_class("result-icon-success")
        self.result_icon.add_css_class("result-icon-failure")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label="Back to Blender")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        try_again_button = Gtk.Button(label="Try again")
        try_again_button.add_css_class("suggested-action")
        try_again_button.add_css_class("pill")
        try_again_button.connect("clicked", self._on_try_again_clicked)
        self.result_button_row.append(try_again_button)

    def _on_try_again_clicked(self, _button):
        self.picker_install_button.set_sensitive(
            self.pending_build is not None or self.pending_zip_path is not None
        )
        self.inner_stack.set_visible_child_name("picker")

    def _open_log_file(self, log_path):
        Gio.AppInfo.launch_default_for_uri(f"file://{log_path}", None)

    def _on_cancel_install_clicked(self, button):
        if not self.install_in_progress or self.install_cancel_requested:
            return
        self.install_cancel_requested = True
        button.set_sensitive(False)
        self.progress_phase_label.set_label("Cancelling…")

        if self._install_process:
            # Extraction/install phase, running through the privileged
            # helper: stop that process the same way Unreal Engine's
            # cancel does.
            self._append_log("Cancelling install, stopping the process and removing partial files…")
            pid = self._install_process.pid

            def on_line(text):
                self._append_log(text)
                return False

            def on_done(_exit_code):
                return False

            run_helper_action("cancel_install", [str(pid)], on_line, on_done)
        else:
            # Download phase: no process to kill, just a background
            # thread reading a socket. It checks this flag on every chunk
            # (see _start_download's worker loop) and stops itself.
            self._append_log("Cancelling download…")

    def _on_launch_just_installed(self, _button):
        if self.last_installed_version:
            install_dir = f"/opt/blender-{self.last_installed_version}"
            self._on_launch_clicked(install_dir)

    def _start_download(self, build):
        self.install_in_progress = True
        self.install_cancel_requested = False
        self.progress_title.set_label(f"Downloading Blender {build['version']}")
        self.progress_subtitle.set_label(f"{build['variant_label']} build, this may take a while")
        buf = self.log_view.get_buffer()
        buf.set_text("")
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_visible(True)
        self.progress_label_row.set_visible(True)
        self.progress_phase_label.set_label("Downloading…")
        self.progress_percent_label.set_label("")
        self._set_progress_buttons_installing()
        self.inner_stack.set_visible_child_name("progress")
        self._append_log(f"Downloading {build['filename']}...")

        # Archive-history builds can be .tar.bz2 or .tar.gz, not just the
        # current .tar.xz convention, so the temp file's suffix has to
        # match the real download, not be assumed.
        filename = build["filename"]
        suffix = next(
            (ext for ext in (".tar.xz", ".tar.bz2", ".tar.gz") if filename.lower().endswith(ext)),
            ".tar.xz",
        )
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="blender-download-")
        os.close(tmp_fd)

        def on_progress(percent):
            self.progress_bar.set_fraction(percent / 100)
            self.progress_percent_label.set_label(f"{percent:.0f}%")
            return False

        def on_done(success, error_message):
            self.progress_label_row.set_visible(False)
            self.progress_bar.set_visible(False)
            if not success:
                if error_message == "cancelled":
                    self._append_log("Download cancelled.")
                    self.progress_title.set_label("Download cancelled")
                    self.progress_subtitle.set_label("The download was stopped")
                else:
                    self._append_log(f"ERROR: download failed: {error_message}")
                    self.progress_title.set_label("Download failed")
                    self.progress_subtitle.set_label(f"Could not download Blender {build['version']}")
                self._set_progress_buttons_failure(None)
                self.install_in_progress = False
                os.remove(tmp_path) if os.path.exists(tmp_path) else None
                return False
            self._append_log("Download complete.")
            self._start_install(tmp_path, cleanup_after=tmp_path)
            return False

        def worker():
            try:
                request = urllib.request.Request(build["url"], headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(request, timeout=30) as response, open(tmp_path, "wb") as out:
                    total = int(response.headers.get("Content-Length", 0))
                    downloaded = 0
                    last_update = 0.0
                    while True:
                        if self.install_cancel_requested:
                            GLib.idle_add(on_done, False, "cancelled")
                            return
                        chunk = response.read(1024 * 256)
                        if not chunk:
                            break
                        out.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if total and (now - last_update >= 0.1):
                            percent = (downloaded * 100) / total
                            GLib.idle_add(on_progress, percent)
                            last_update = now
                GLib.idle_add(on_done, True, None)
            except Exception as exc:
                GLib.idle_add(on_done, False, str(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _start_install(self, zip_path, cleanup_after=None):
        if not self.install_in_progress:
            self.install_in_progress = True
        self.install_cancel_requested = False
        self.progress_title.set_label(f"Installing {os.path.basename(zip_path)}")
        self.progress_subtitle.set_label("This requires administrator privileges and may take a while")
        buf = self.log_view.get_buffer()
        if cleanup_after is None:
            buf.set_text("")
        self.progress_bar.set_fraction(0.0)
        self.progress_bar.set_visible(True)
        self.progress_label_row.set_visible(True)
        self.progress_phase_label.set_label("Starting…")
        self.progress_percent_label.set_label("")
        self._set_progress_buttons_installing()
        self.inner_stack.set_visible_child_name("progress")

        log_path, log_file = log_util.open_log_file("install-blender")
        self._append_log(f"Log file: {log_path}")
        self._append_log(f"$ pkexec helper install_blender {zip_path}")
        log_file.write(f"$ pkexec helper install_blender {zip_path}\n")
        log_file.flush()

        def on_line(text):
            if text.startswith("PROGRESS "):
                try:
                    percent = float(text.split(" ", 1)[1])
                except ValueError:
                    percent = None
                if percent is not None:
                    self.progress_bar.set_fraction(percent / 100)
                    self.progress_phase_label.set_label("Extracting archive…")
                    self.progress_percent_label.set_label(f"{percent:.0f}%")
                log_file.write(text + "\n")
                log_file.flush()
                return False

            if text.startswith("INSTALLED_VERSION="):
                self.last_installed_version = text.split("=", 1)[1]
                log_file.write(text + "\n")
                log_file.flush()
                return False

            if text.startswith("Installing "):
                self.progress_phase_label.set_label("Installing…")
                self.progress_percent_label.set_label("")
            elif text.startswith("Setting ownership"):
                self.progress_phase_label.set_label("Setting ownership…")
                self.progress_percent_label.set_label("")

            self._append_log(text)
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                version = self.last_installed_version
                self.progress_title.set_label("Installation complete")
                if version:
                    self.progress_subtitle.set_label(
                        f"Blender {version} was installed to /opt/blender-{version}"
                    )
                else:
                    self.progress_subtitle.set_label("Blender was installed")
                self.progress_bar.set_visible(False)
                self.progress_label_row.set_visible(False)
                self._set_progress_buttons_success(log_path)
            elif self.install_cancel_requested:
                self.progress_title.set_label("Installation cancelled")
                self.progress_subtitle.set_label("The install was stopped and its partial files removed")
                self.progress_bar.set_visible(False)
                self.progress_label_row.set_visible(False)
                self._set_progress_buttons_failure(log_path)
            else:
                version = self.last_installed_version
                label = f"Blender {version}" if version else "Blender"
                self.progress_title.set_label("Installation failed")
                self.progress_subtitle.set_label(f"Something went wrong while installing {label}")
                self.progress_bar.set_visible(False)
                self.progress_label_row.set_visible(False)
                self._set_progress_buttons_failure(log_path)
            self.pending_zip_path = None
            self.pending_build = None
            self.install_in_progress = False
            self._install_process = None
            if cleanup_after and os.path.exists(cleanup_after):
                os.remove(cleanup_after)
            self.refresh()
            return False

        self._install_process = run_helper_action("install_blender", [zip_path], on_line, on_done)

    def _classify_log_line(self, text):
        lower = text.lower()
        if text.startswith("$") or text.startswith("Log file:"):
            return self.log_tag_command
        if text.startswith("[exited with code "):
            return self.log_tag_success if text.strip() == "[exited with code 0]" else self.log_tag_error
        if "error" in lower or "failed" in lower:
            return self.log_tag_error
        if "warn" in lower:
            return self.log_tag_warn
        if lower.startswith("done.") or "installed" in lower or lower.startswith("download"):
            return self.log_tag_success
        if text.startswith("Extracted ") and "files (" in text:
            return self.log_tag_progress
        if text and text[0].isupper() and text.endswith("...") or text.startswith("Ensuring") \
                or text.startswith("Installing") or text.startswith("Setting ownership"):
            return self.log_tag_info
        return None

    def _append_log(self, text):
        buf = self.log_view.get_buffer()
        tag = self._classify_log_line(text)
        end_iter = buf.get_end_iter()
        if tag is not None:
            buf.insert_with_tags(end_iter, text + "\n", tag)
        else:
            buf.insert(end_iter, text + "\n")
        end_mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(end_mark, 0.0, False, 0.0, 0.0)
        return False


class FlatpakAppPage(Gtk.Box):
    """Simple single-app install/launch pattern, shared by both Flatpak
    apps: no multi-version, no picker screen, just Install/Launch/
    Uninstall against one Flathub app id."""

    def __init__(self, app_entry, description, on_state_changed):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app_entry = app_entry
        self.description = description
        self.on_state_changed = on_state_changed
        self._pending_dialog = None
        self.install_in_progress = False

        self.inner_stack = Gtk.Stack()
        self.toast_overlay = Adw.ToastOverlay()
        self.toast_overlay.set_child(self.inner_stack)
        self.append(self.toast_overlay)

        self._build_main_page()
        self._build_progress_page()

        self.inner_stack.set_visible_child_name("main")
        self.refresh()

    def go_to_list(self):
        self.inner_stack.set_visible_child_name("main")

    # --- main page ---------------------------------------------------

    def _build_main_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        icon_widget = make_app_icon_widget(self.app_entry, size=56)
        icon_widget.set_halign(Gtk.Align.CENTER)
        page.append(icon_widget)

        title = Gtk.Label(label=self.app_entry.name)
        title.add_css_class("title-1")
        title.set_halign(Gtk.Align.CENTER)
        page.append(title)

        subtitle = Gtk.Label(label=self.description)
        subtitle.add_css_class("dim-label")
        subtitle.set_wrap(True)
        subtitle.set_justify(Gtk.Justification.CENTER)
        subtitle.set_halign(Gtk.Align.CENTER)
        page.append(subtitle)

        self.status_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.append(self.status_box)

        self.note_label = Gtk.Label()
        self.note_label.add_css_class("dim-label")
        self.note_label.add_css_class("caption")
        self.note_label.set_wrap(True)
        self.note_label.set_justify(Gtk.Justification.CENTER)
        self.note_label.set_halign(Gtk.Align.CENTER)
        page.append(self.note_label)

        self.button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.button_row.set_halign(Gtk.Align.CENTER)
        page.append(self.button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(640)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "main")

    def refresh(self):
        child = self.status_box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.status_box.remove(child)
            child = next_child

        installed = flatpak_state.is_installed(self.app_entry.flatpak_id)
        self.status_box.append(self._build_status_row(installed))

        child = self.button_row.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.button_row.remove(child)
            child = next_child

        if installed:
            self.note_label.set_label("")
            launch_button = Gtk.Button(label="Launch")
            launch_button.add_css_class("pill")
            launch_button.connect("clicked", self._on_launch_clicked)
            self.button_row.append(launch_button)

            uninstall_button = Gtk.Button(label="Uninstall")
            uninstall_button.add_css_class("destructive-action")
            uninstall_button.add_css_class("pill")
            uninstall_button.connect("clicked", self._on_uninstall_clicked)
            self.button_row.append(uninstall_button)
        else:
            self.note_label.set_label(
                "We just install/launch this app. Project management happens inside "
                f"{self.app_entry.name} itself, not in Linux Art Studio."
            )
            install_button = Gtk.Button(label="Install")
            install_button.add_css_class("suggested-action")
            install_button.add_css_class("pill")
            install_button.connect("clicked", self._on_install_clicked)
            self.button_row.append(install_button)

        self.on_state_changed(installed)

    def _build_status_row(self, installed):
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(12)
        box.set_margin_end(12)

        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        meta_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        meta_box.set_hexpand(True)
        name_label = Gtk.Label(label=self.app_entry.name)
        name_label.set_halign(Gtk.Align.START)
        name_label.add_css_class("heading")
        meta_box.append(name_label)
        desc_label = Gtk.Label(label="Installed via Flatpak (Flathub)" if installed
                                else "Installed via Flatpak (Flathub), not installed yet")
        desc_label.set_halign(Gtk.Align.START)
        desc_label.add_css_class("caption")
        desc_label.add_css_class("dim-label")
        meta_box.append(desc_label)
        header_row.append(meta_box)

        if installed:
            status_pill = Gtk.Label(label="Installed")
            status_pill.add_css_class("status-pill-installed")
            status_pill.set_valign(Gtk.Align.START)
            header_row.append(status_pill)

        box.append(header_row)
        frame.set_child(box)
        return frame

    def _on_launch_clicked(self, _button):
        Gio.AppInfo.create_from_commandline(
            f"flatpak run {self.app_entry.flatpak_id}", None, Gio.AppInfoCreateFlags.NONE
        ).launch(None, None)

    def _on_uninstall_clicked(self, _button):
        # See PENDING_DIALOG_RULE at the top of this file.
        dialog = Adw.AlertDialog(
            heading=f"Uninstall {self.app_entry.name}?",
            body="This removes the Flatpak installation.",
        )
        self._pending_dialog = dialog
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("uninstall", "Uninstall")
        dialog.set_response_appearance("uninstall", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(_dlg, response):
            self._pending_dialog = None
            if response == "uninstall":
                self._run_uninstall()

        dialog.connect("response", on_response)
        dialog.present(self.get_root())

    def _run_uninstall(self):
        self.toast_overlay.add_toast(Adw.Toast.new(f"Uninstalling {self.app_entry.name}…"))

        log_path, log_file = log_util.open_log_file(f"uninstall-{self.app_entry.app_id}")
        log_file.write(f"$ pkexec helper uninstall_flatpak_app {self.app_entry.flatpak_id}\n")
        log_file.flush()

        def on_line(text):
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                self.toast_overlay.add_toast(Adw.Toast.new(f"{self.app_entry.name} uninstalled."))
            else:
                toast = Adw.Toast.new(
                    f"Failed to uninstall {self.app_entry.name} (exit code {exit_code})"
                )
                toast.set_button_label("View log")
                toast.connect("button-clicked", lambda _t: self._open_log_file(log_path))
                self.toast_overlay.add_toast(toast)
            self.refresh()
            return False

        run_helper_action("uninstall_flatpak_app", [self.app_entry.flatpak_id], on_line, on_done)

    # --- install flow, progress page --------------------------------------

    def _on_install_clicked(self, _button):
        if self.install_in_progress:
            self.inner_stack.set_visible_child_name("progress")
            return
        self._start_install()

    def _build_progress_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        page.set_margin_top(24)
        page.set_margin_bottom(24)
        page.set_margin_start(24)
        page.set_margin_end(24)

        self.result_icon = Gtk.Label(label="")
        self.result_icon.set_size_request(56, 56)
        self.result_icon.set_halign(Gtk.Align.CENTER)
        self.result_icon.set_visible(False)
        page.append(self.result_icon)

        self.progress_title = Gtk.Label(label=f"Installing {self.app_entry.name}")
        self.progress_title.add_css_class("title-1")
        self.progress_title.set_wrap(True)
        self.progress_title.set_justify(Gtk.Justification.CENTER)
        self.progress_title.set_halign(Gtk.Align.CENTER)
        self.progress_title.set_max_width_chars(40)
        page.append(self.progress_title)

        self.progress_subtitle = Gtk.Label(label="This requires administrator privileges and may take a while")
        self.progress_subtitle.add_css_class("dim-label")
        self.progress_subtitle.set_wrap(True)
        self.progress_subtitle.set_justify(Gtk.Justification.CENTER)
        self.progress_subtitle.set_halign(Gtk.Align.CENTER)
        page.append(self.progress_subtitle)

        log_frame = Gtk.Frame()
        log_frame.add_css_class("card")
        log_frame.set_vexpand(True)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_top_margin(10)
        self.log_view.set_bottom_margin(10)
        self.log_view.set_left_margin(10)
        self.log_view.set_right_margin(10)
        self.log_view.add_css_class("terminal-log")
        scroller.set_child(self.log_view)
        log_frame.set_child(scroller)

        log_buf = self.log_view.get_buffer()
        self.log_tags = {
            "command": log_buf.create_tag("command", foreground="#8a8a8a"),
            "error": log_buf.create_tag("error", foreground="#ff6961"),
            "success": log_buf.create_tag("success", foreground="#8ff0a4"),
            "warn": log_buf.create_tag("warn", foreground="#f8e45c"),
            "info": log_buf.create_tag("info", foreground="#7ab8ff"),
        }
        page.append(log_frame)

        self.result_button_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.result_button_row.set_halign(Gtk.Align.CENTER)
        page.append(self.result_button_row)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(900)
        clamp.set_child(page)
        self.inner_stack.add_named(clamp, "progress")

        self._set_progress_buttons_installing()

    def _clear_result_buttons(self):
        child = self.result_button_row.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self.result_button_row.remove(child)
            child = next_child

    def _set_progress_buttons_installing(self):
        self._clear_result_buttons()
        self.result_icon.set_visible(False)

    def _set_progress_buttons_success(self):
        self._clear_result_buttons()
        self.result_icon.set_label("✓")
        self.result_icon.remove_css_class("result-icon-failure")
        self.result_icon.add_css_class("result-icon-success")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label=f"Back to {self.app_entry.name}")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        launch_button = Gtk.Button(label="Launch")
        launch_button.add_css_class("suggested-action")
        launch_button.add_css_class("pill")
        launch_button.connect("clicked", self._on_launch_clicked)
        self.result_button_row.append(launch_button)

    def _set_progress_buttons_failure(self):
        self._clear_result_buttons()
        self.result_icon.set_label("✕")
        self.result_icon.remove_css_class("result-icon-success")
        self.result_icon.add_css_class("result-icon-failure")
        self.result_icon.set_visible(True)

        done_button = Gtk.Button(label=f"Back to {self.app_entry.name}")
        done_button.add_css_class("pill")
        done_button.connect("clicked", lambda _b: self.go_to_list())
        self.result_button_row.append(done_button)

        try_again_button = Gtk.Button(label="Try again")
        try_again_button.add_css_class("suggested-action")
        try_again_button.add_css_class("pill")
        try_again_button.connect("clicked", lambda _b: self._start_install())
        self.result_button_row.append(try_again_button)

    def _open_log_file(self, log_path):
        Gio.AppInfo.launch_default_for_uri(f"file://{log_path}", None)

    def _start_install(self):
        self.install_in_progress = True
        self.progress_title.set_label(f"Installing {self.app_entry.name}")
        self.progress_subtitle.set_label("This requires administrator privileges and may take a while")
        buf = self.log_view.get_buffer()
        buf.set_text("")
        self._set_progress_buttons_installing()
        self.inner_stack.set_visible_child_name("progress")

        log_path, log_file = log_util.open_log_file(f"install-{self.app_entry.app_id}")
        self._append_log(f"Log file: {log_path}")
        self._append_log(f"$ pkexec helper install_flatpak_app {self.app_entry.flatpak_id}")
        log_file.write(f"$ pkexec helper install_flatpak_app {self.app_entry.flatpak_id}\n")
        log_file.flush()

        def on_line(text):
            self._append_log(text)
            log_file.write(text + "\n")
            log_file.flush()
            return False

        def on_done(exit_code):
            log_file.write(f"[exited with code {exit_code}]\n")
            log_file.close()
            if exit_code == 0:
                self.progress_title.set_label("Installation complete")
                self.progress_subtitle.set_label(f"{self.app_entry.name} was installed")
                self._set_progress_buttons_success()
            else:
                self.progress_title.set_label("Installation failed")
                self.progress_subtitle.set_label(f"Something went wrong while installing {self.app_entry.name}")
                self._set_progress_buttons_failure()
            self.install_in_progress = False
            self.refresh()
            return False

        run_helper_action("install_flatpak_app", [self.app_entry.flatpak_id], on_line, on_done)

    def _classify_log_line(self, text, tags):
        lower = text.lower()
        if text.startswith("$") or text.startswith("Log file:"):
            return tags["command"]
        if text.startswith("[exited with code "):
            return tags["success"] if text.strip() == "[exited with code 0]" else tags["error"]
        if "error" in lower or "failed" in lower:
            return tags["error"]
        if "warn" in lower:
            return tags["warn"]
        if lower.startswith("done.") or "installed" in lower:
            return tags["success"]
        if text and text[0].isupper() and text.endswith("..."):
            return tags["info"]
        return None

    def _append_log(self, text):
        buf = self.log_view.get_buffer()
        tag = self._classify_log_line(text, self.log_tags)
        end_iter = buf.get_end_iter()
        if tag is not None:
            buf.insert_with_tags(end_iter, text + "\n", tag)
        else:
            buf.insert(end_iter, text + "\n")
        end_mark = buf.create_mark(None, buf.get_end_iter(), False)
        self.log_view.scroll_to_mark(end_mark, 0.0, False, 0.0, 0.0)
        return False


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Creative Installer")
        self.set_default_size(860, 640)

        install_panel_css()

        toolbar_view = Adw.ToolbarView()
        header = Adw.HeaderBar()

        self.sidebar_toggle_button = Gtk.ToggleButton()
        self.sidebar_toggle_button.set_icon_name("sidebar-show-symbolic")
        self.sidebar_toggle_button.set_tooltip_text("Collapse category sidebar to icons")
        self.sidebar_toggle_button.connect("toggled", self._on_sidebar_toggle)
        header.pack_start(self.sidebar_toggle_button)

        about_button = Gtk.Button()
        about_button.set_icon_name("help-about-symbolic")
        about_button.set_tooltip_text("About Linux Art Studio")
        about_button.connect("clicked", self._on_about_clicked)
        header.pack_end(about_button)

        toolbar_view.add_top_bar(header)

        split = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)

        self.categories = apps_by_category()
        category_names = [name for name, apps in self.categories.items() if apps]

        self.stack = Gtk.Stack()
        self.pages = {}

        # Panel 1: categories. Always visible, same layout at any window
        # size, maximized or not: no responsive collapse. A narrow/wide
        # mode that rebuilt panel 2 and juggled which app/category was
        # "active" across the transition was a real source of real bugs
        # (apps jumping to a different app's page, or losing their own
        # sub-page, on resize) and wasn't worth the complexity it added.
        self.panel1 = CategoryListPanel(category_names, self._on_category_selected)
        self.panel1.set_size_request(220, -1)
        self.panel1.set_hexpand(False)
        self.panel1.add_css_class("panel-categories")
        self.separator1 = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)

        # Panel 2: apps within the selected category
        self.app_list_panel = AppListPanel(self._on_app_selected)
        self.app_list_panel.set_size_request(260, -1)
        self.app_list_panel.set_hexpand(False)
        self.app_list_panel.add_css_class("panel-apps")

        # Panel 3: the selected app's own page
        unreal_page = UnrealPage(self._on_unreal_state_changed)
        self.stack.add_named(unreal_page, "ue")
        self.pages["ue"] = unreal_page

        davinci_page = DaVinciPage(self._on_davinci_state_changed)
        self.stack.add_named(davinci_page, "dr")
        self.pages["dr"] = davinci_page

        blender_page = BlenderPage(self._on_blender_state_changed)
        self.stack.add_named(blender_page, "blender")
        self.pages["blender"] = blender_page

        # Every other Flatpak-backed app shares the one simple
        # install/launch/uninstall pattern, so it's built generically from
        # apps.py rather than one hardcoded block per app.
        for app in APPS:
            if app.app_id in self.pages or not app.flatpak_id:
                continue
            flatpak_page = FlatpakAppPage(
                app, app.description,
                lambda installed, aid=app.app_id: self._on_flatpak_state_changed(aid, installed),
            )
            self.stack.add_named(flatpak_page, app.app_id)
            self.pages[app.app_id] = flatpak_page

        self.stack.set_hexpand(True)

        split.append(self.panel1)
        split.append(self.separator1)
        split.append(self.app_list_panel)
        split.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        split.append(self.stack)

        toolbar_view.set_content(split)
        self.set_content(toolbar_view)

        self.panel1.select_first()
        self.sidebar_toggle_button.set_active(True)  # compact by default
        self._refresh_sidebar_subtitles()

    def _refresh_sidebar_subtitles(self):
        # Flatpak-backed pages (FlatpakAppPage) already push their own
        # correct initial subtitle during construction (each one calls
        # refresh() -> on_state_changed() at the end of its own __init__,
        # which happens before this runs), so only Unreal/DaVinci/Blender
        # need an explicit refresh here.
        self._on_unreal_state_changed(len(unreal_state.list_installed_versions()))
        self._on_davinci_state_changed(resolve_state.is_installed())
        self._on_blender_state_changed(len(blender_state.list_installed_versions()))

    def _on_category_selected(self, category_name):
        self.app_list_panel.show_apps(self.categories.get(category_name, []))

    def _on_sidebar_toggle(self, button):
        compact = button.get_active()
        self.panel1.set_compact(compact)
        width = CategoryListPanel.COMPACT_WIDTH if compact else CategoryListPanel.EXPANDED_WIDTH
        self.panel1.set_size_request(width, -1)
        self.sidebar_toggle_button.set_tooltip_text(
            "Expand category sidebar" if compact else "Collapse category sidebar to icons"
        )

    def _on_about_clicked(self, _button):
        about = Adw.AboutDialog()
        about.set_application_name("Linux Art Studio")
        about.set_application_icon("linux-art-studio")
        about.set_version(APP_VERSION)
        about.set_comments(
            "A local, unprivileged GTK4/libadwaita installer for creative and game-dev "
            "tools on Fedora Linux."
        )
        about.set_developer_name("vrm")
        about.set_developers(["vrm"])
        about.set_license_type(Gtk.License.GPL_3_0)
        about.set_website("https://github.com/learncad07-hub/linux-art-studio")
        about.set_issue_url("https://github.com/learncad07-hub/linux-art-studio/issues")
        about.present(self)

    def _on_app_selected(self, app_id):
        if app_id in self.pages:
            # Only an actual switch to a different app resets it to its
            # list view; re-selecting the already-active app (shouldn't
            # normally even fire, GTK doesn't re-emit row-selected for
            # clicking an already-selected row) must not wipe out
            # whatever sub-page it was on.
            already_active = self.stack.get_visible_child_name() == app_id
            self.stack.set_visible_child_name(app_id)
            page = self.pages[app_id]
            if not already_active and hasattr(page, "go_to_list"):
                page.go_to_list()

    def _on_unreal_state_changed(self, version_count):
        text = "Not installed" if version_count == 0 else (
            "1 version installed" if version_count == 1 else f"{version_count} versions installed"
        )
        self.app_list_panel.set_subtitle("ue", text)

    def _on_davinci_state_changed(self, installed):
        self.app_list_panel.set_subtitle("dr", "Installed" if installed else "Not installed")

    def _on_blender_state_changed(self, version_count):
        text = "Not installed" if version_count == 0 else (
            "1 version installed" if version_count == 1 else f"{version_count} versions installed"
        )
        self.app_list_panel.set_subtitle("blender", text)

    def _on_flatpak_state_changed(self, app_id, installed):
        self.app_list_panel.set_subtitle(app_id, "Installed" if installed else "Not installed")
