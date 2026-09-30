# projects

One fuzzy picker for local and SSH Git repositories and worktrees. Run `p`, pick
a context, open your editor. Recently opened contexts come first.

```text
laptop  · website › main
workbox · api     › feature-search
laptop  · website › fix-checkout
```

## Install

Requires macOS or Linux, Python 3.9+, Git 2.36+, and fzf 0.67.0+ for interactive selection.
SSH is required for remote machines; Herdr 0.8.2+ is optional.

```sh
git clone https://github.com/gertyhiler/projects.git ~/projects-cli
mkdir -p ~/.local/bin ~/.config/projects
ln -s ~/projects-cli/p ~/.local/bin/p
cp ~/projects-cli/config.example.json ~/.config/projects/config.json
```

Ensure `~/.local/bin` is on PATH. Edit the configuration: give each machine a
stable unique `host` name, set its discovery `roots`, and remove unused remotes.
Install and configure projects on each remote too. A remote's `name` must equal
that machine's configured `host`; `ssh` is an alias from your existing SSH config.
No SSH credentials are stored by projects.

## Usage

```sh
p                  # choose and open
p checkout         # start with a fuzzy query
p --refresh        # refresh the catalog without opening anything
p --list           # inspect cached catalog
p --json --local   # discover this machine, without cache/history writes
```

In plain navigation, Escape cancels. The initial scan and `--refresh` contact configured SSH machines;
subsequent calls immediately show the saved catalog, even when it is stale.
After five minutes, the next call schedules one detached background refresh.
The picker never waits for that refresh; new entries appear on the next call.
`p --refresh` explicitly waits for an update. The first run without a cache must
finish discovery before it can show a list. Opening/editor/session settings do
not invalidate the catalog cache. A failed remote refresh retains its old
entries with an offline marker. Selecting one retries the connection. Local
projects remain usable. A deleted directory produces an error; refresh to remove
it. Discovery is bounded to five directory levels by default, skips dependencies,
and gets all linked worktrees from Git even when they live outside the roots.
Independent roots and Git queries use a bounded thread pool, with one worktree
query per common repository. Local discovery runs alongside remote SSH queries.

`p` opens a child editor process; it does not change the calling shell's directory.

## Configuration

`$XDG_CONFIG_HOME/projects/config.json`, default `~/.config/projects/config.json`.
`PROJECTS_CONFIG` overrides that path on the current machine.

| Key | Default | Purpose |
| --- | --- | --- |
| `host` | `local` | Stable identity of this machine; set explicitly for SSH use |
| `roots` | `~/Projects`, `~/code` | Directories to search |
| `max_depth` | `5` | Maximum search depth below each root |
| `cache_seconds` | `300` | Age at which a background refresh is scheduled |
| `scan_workers` | `4` | Local root/Git worker limit (1–16) |
| `ssh_timeout` | `12` | SSH connection timeout in seconds |
| `opener` | `["nvim", "."]` | Command argv, run in the chosen directory |
| `backend` | `direct` | `direct` editor or `herdr` workspace |
| `herdr_session` | `projects` | Named session used outside Herdr |
| `remotes` | `[]` | Objects with `name`, `ssh`, optional `command`, `backend`, `herdr_session` |

Remote `command` defaults to `~/.local/bin/p`; remote `backend` defaults to
`herdr`. Commands use argv arrays, not shell string interpolation. `{path}` in an
opener argument is replaced with the selected absolute path. Only configure
trusted opener commands and SSH hosts.

History is stored in `$XDG_DATA_HOME/projects/history.json`, cache in
`$XDG_CACHE_HOME/projects/`. These follow standard home-directory fallbacks and
are not synchronized between machines. Background refresh diagnostics are stored
in the matching cache `.log` file. Refreshes share a process lock and replace the
JSON cache atomically. A context is identified by machine and
canonical path. History is updated after successful direct editor exit or Herdr
workspace preparation. Discovery does not read repository file contents.

## Picker appearance and navigation

Add a `ui` object to your configuration. Each setting is independent:

```json
"ui": {
  "style": "accented",
  "navigation": "vim",
  "icons": false,
  "show_path": false,
  "show_help": true
}
```

| Setting | Default | Options |
| --- | --- | --- |
| `style` | `plain` | `plain` (no colors), `accented` (machine/project/branch accents) |
| `navigation` | `plain` | `plain`, `vim` |
| `icons` | `false` | Enable Nerd Font icons explicitly; requires a Nerd Font in your terminal |
| `show_path` | `true` | Show the full path |
| `show_help` | `true` | Show key hints; Vim mode indicator always remains visible |

Plain navigation keeps normal fzf search and arrow keys; Esc cancels.
Vim navigation starts in INSERT. Esc switches to NORMAL and hides the input,
preserving both the query and selection. `i` or `/` resumes INSERT.
In NORMAL, `j`/`k` and arrows move, `g`/`G` jump to first/last,
Ctrl-u/d move half a page, Ctrl-b/f move a full page, and `q` cancels.
Enter opens the selected context in either mode. `h`/`l` are unassigned.
When paths are hidden, otherwise identical contexts show a unique path suffix.
UI changes do not invalidate the catalog cache.

Missing or older fzf produces installation instructions before discovery starts.
`--list`, `--json` and `--refresh` work without fzf. Projects does not install it.

## Shell functions

Standalone `gw` (pick and cd), `gwn` (pick and open Neovim), and `gwl` (list) can
live in your dotfiles independently of projects. Let `gwn` accept an optional path
so projects can reuse it without a second picker. For example:

```json
{
  "opener": ["zsh", "-f", "-c", "source ~/.config/zsh/projects.zsh; gwn \"$1\"", "projects", "{path}"]
}
```

For Fish, explicitly set your function directory without loading startup files:

```json
{
  "opener": ["fish", "--no-config", "-c", "set -p fish_function_path ~/.config/fish/functions; gwn $argv[1]", "{path}"]
}
```

## Herdr

Outside Herdr, a remote selection prepares a workspace in the remote `projects`
session, then attaches with `herdr --remote <host> --session projects`. Set `herdr_session` on the remote entry to choose another session name.
A local `herdr` backend uses the same approach locally. Herdr may create its own
initial workspace when starting a new server.

Inside Herdr, local selections focus/create a workspace in the current session.
Selecting a remote there uses a direct SSH editor instead of a nested Herdr client.
Projects only reuses its own labeled workspaces and only sends a startup command
to a newly created pane. Selecting an existing workspace never types into it or
restarts its editor. If you close the editor, relaunch it in that workspace.
If startup command submission fails, its workspace keeps a `(starting)` label
and is not reused; retry creates a new workspace. Herdr paths must be UTF-8.

## Dotfiles submodule

```sh
git submodule add https://github.com/gertyhiler/projects.git projects
ln -s "$HOME/dotfiles/projects/p" "$HOME/.local/bin/p"
```

Update with `git submodule update --init projects`. Updating the pinned revision
is an explicit dotfiles change. Back up existing files before creating symlinks.

## Development

```sh
python3 -m unittest discover -s tests -v
```

Tests use temporary Git repositories, fake process responses and isolated state.
They do not connect to your SSH hosts or modify existing Herdr sessions.

MIT license.
