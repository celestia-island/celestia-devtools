"""Release-lane auditing: reconcile a repository's release tags against its registries.

The rule this package reads is ``docs/agent-workflow-convention.md`` rule 4:
a version is a release-lane property, its tag is part of the release, and a
lane is append-only. The implementation lives in
:mod:`celestia_devtools.release.audit`; the ``release-audit`` command targets
its ``main()`` directly. This module deliberately does **not** re-export
anything, because that would trigger runpy's "found in sys.modules" warning for
``python -m celestia_devtools.release.audit``.
"""
