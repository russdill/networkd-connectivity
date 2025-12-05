Non-Python resources used at deploy/runtime.  Nothing here is `import`ed; it
is shipped verbatim by either:

* **pip/wheel** via `package-data` in `pyproject.toml` so users can copy or
  symlink at install time.
* **Debian packages** pick and choose via `debian/*.install` into
  `/usr/lib/networkd-connectivity/<component>/`.

Subdirectories map 1-to-1 with optional components (`snmp`, `routemon`, ...).
