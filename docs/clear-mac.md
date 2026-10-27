# Clear Dock items and desktop widgets

`runner-bootstrap` contains the desktop cleanup commands and runs them directly
when the Dock or desktop needs clearing. There is no `clear-mac` script or
formula to install or approve. `--skip-clear-desktop` opts out.

```sh
brew update
brew install joeblau/hb/runner-setup
runner-bootstrap
```

Bootstrap also configures the host as described in [boot-startup.md](boot-startup.md).
Run it as the user whose desktop you want to clear, without `sudo`. The cleanup
clears pinned apps, files and folders from the Dock, hides recent apps, and
hides all desktop widgets in both the standard desktop and Stage Manager.
Widget layouts are retained; Notification Center widgets are preserved.
These are the [Show Widgets settings](https://support.apple.com/guide/mac-help/add-and-customize-widgets-mchl52be5da5/mac)
in System Settings > Desktop & Dock. Requires macOS Sonoma or later.

Both preference domains are backed up before any changes under
`~/Library/Application Support/clear-mac/backups/`. The command prints restore
commands and refreshes that user's Dock and Notification Center. Finder, Trash,
running apps and minimized windows can still appear. Bootstrap stops if a
backup or preference write fails; completed backups remain available to restore.

The standalone `clear-mac` formula and the legacy `clear-dock` rename have
been removed. Backups from either command remain in their original directories.
