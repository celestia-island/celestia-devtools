"""Production-target lifecycle commands (single-host deploy toolchain).

Layering contract (vs. ``celestia_devtools.env``):

* ``env/`` owns **development** machines — foreground process supervision,
  embedded PG, WSL/QEMU/docker bring-up;
* ``deploy/`` owns **production targets** — a fresh box goes from zero to a
  systemd-resident service, then gets verified, upgraded, backed up. The
  commands never hold a foreground process and never touch mock stacks.

Cross-host deployment scripts live here and only here; a repo that needs its
own deployable artifact (e.g. an OS image builder) must register the reason in
its PR description.
"""
