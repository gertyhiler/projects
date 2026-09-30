# Shell functions and a portable CLI

Status: accepted (implementation direction approved by the project owner)

The projects CLI owns machine-aware discovery, fuzzy selection, recent history,
and opening adapters. It uses Python's standard library, Git, fzf and SSH.
Standalone Zsh/Fish worktree functions remain in the consuming dotfiles repository;
they work without this CLI. An argv-based opener can invoke those functions with
an explicit path, avoiding a second picker and avoiding interactive shell startup.
The CLI has a default Neovim opener for users without shell functions.

Machine-specific configuration, caches and history are untracked XDG files.
Contexts use stable machine names and canonical absolute paths. History is local
to each installation. Dotfiles pins this project as a Git submodule.

Herdr is an optional opening backend. Only workspaces created by projects are
reused, and commands are sent only to a newly created pane. Inside Herdr, local
selection uses the current session; remote selection uses plain SSH to avoid
nested clients. Outside Herdr, remote opening prepares a named remote session
before attaching. No existing user session is stopped or reset.
