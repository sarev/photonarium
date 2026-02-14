# Release Notes

## v1.0.0-beta.3

### Mobile & Responsive

- **Hamburger menu:** On narrow screens (<=768px), the toolbar collapses into a compact bar showing the logo, screen title, theme toggle, and a hamburger button. Tapping the hamburger reveals the full toolbar controls as a vertical dropdown; tapping outside or navigating to a different screen closes it. The menu auto-closes when resizing back to desktop width.
- **Collapsible info panel:** A toggle button at the edge of the gallery info panel lets you collapse it to reclaim horizontal space. The panel auto-collapses when it would take more than 20% of the viewport width (e.g. on narrow windows or tablets). Once you explicitly toggle the panel, auto-collapse stops overriding your choice. The preference persists across sessions.
- **Dynamic viewport height:** The app and mobile info panel now use `dvh` units (with `vh` fallback) so they correctly resize when the mobile browser address bar appears or disappears — fixing the "info panel stuck at half height after rotation" bug.
- **Wider mobile scrollbars:** Scrollbar touch targets are wider (16px) on mobile for easier dragging. Firefox scrollbar styling is now also supported via the standard `scrollbar-width`/`scrollbar-color` properties.

## v1.0.0-beta.2

### LAN Access

Photonarium is now accessible from other devices on your local network. The server binds to all network interfaces (`0.0.0.0`) by default, so you can browse your photo library from a phone, tablet, or another computer on the same network.

To restrict access to the machine Photonarium is running on, set `server_host: 127.0.0.1` in your config file. Photonarium is designed for trusted home networks and should not be exposed to the public internet.

### Configuration Relocated to OS-Standard Location

The configuration file has moved from `.photonarium.yml` inside the data directory to the OS-standard location:

- **Windows:** `%LOCALAPPDATA%\Photonarium\photonarium.yml`
- **macOS:** `~/Library/Application Support/Photonarium/photonarium.yml`
- **Linux:** `~/.config/photonarium/photonarium.yml`

The config file now stores a `data_dir` setting, so after installation `python app.py` just works — no need to pass `--data-dir` every time.

**Existing users:** If Photonarium finds a `.photonarium.yml` in the current directory but no config at the new location, it will automatically migrate your settings and inject the correct `data_dir`. The old file is left in place but ignored.

### In-App Settings Editor

The **Edit Settings** button on the Database screen now opens an in-app settings editor. The editor works from any device on your network - no need for local file access.

- **Schema-driven:** The backend describes all fields, types, numeric constraints, and help text in a single API response. The frontend renders a generic form with zero hardcoded knowledge of individual settings.
- **Danger fields:** Settings that could break connectivity (`data_dir`, `server_host`, `server_port`) are highlighted with a red border and warning icon.
- **Validation:** Client-side range checking plus full backend validation on save, with clear error messages.
- **Restart required:** Saved changes are written to disk but don't take effect until Photonarium is restarted. The dialog shows the on-disk values, so re-opening after a save reflects what was saved.
- **Direct editing:** A link in the dialog header lets you reveal the YAML file in your file manager if you prefer editing it by hand.

### Installation Improvements

- The installer now creates the config file at the OS-standard location with `data_dir` pre-configured, so the final startup command is simply `python app.py`.
- New `--init-config <data-dir>` flag for scripted/automated installs.
- New `--config` / `-c` flag to use a config file at a custom location.

### Bug Fixes

- **Search filter button:** The Clear Filter button on the search screen now works correctly (was broken by a duplicate HTML element ID).
- **Touch panning:** One-finger panning now works when zoomed in full-screen view, matching the existing mouse drag behaviour.
- **Filter scroll position:** Applying a new filter or opening a group now scrolls the gallery to the top instead of staying at a stale position.
- **Histogram errors:** Requesting a histogram for an image with no checksum now returns a clean 404 instead of a 500 error.
- **Fullscreen performance:** The gallery info panel (including on-demand histogram generation) is deferred while full-screen view is open, reducing unnecessary work.
- **Group navigation:** A loading overlay is now shown when opening a group from the Groups screen or navigating between groups.
- **Error messages:** API errors from the backend now show clearer messages when the response is not valid JSON.
- **Accessibility:** Full-screen images now have alt text set to the filename.
- **Config alignment:** The default similarity threshold for level 2 duplicates now matches between the config template and the internal default (0.93).
