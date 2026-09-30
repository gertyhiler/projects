#!/bin/sh
# Resolve the Python entry point even when p itself is a symlink.
exec python3 -c 'import os,runpy,sys; target=os.path.realpath(sys.argv.pop(1)); sys.path.insert(0, os.path.dirname(target)); runpy.run_path(os.path.join(os.path.dirname(target), "projects.py"), run_name="__main__")' "$0" "$@"
