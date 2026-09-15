"""CI governance tools: audit and bulk-fill ``timeout-minutes`` on GitHub Actions jobs.

The implementation lives in :mod:`celestia_devtools.ci.job_timeouts`; the
``celestia-job-timeouts`` entry point targets its ``main()`` directly. This module
deliberately does **not** re-export anything, because that would trigger runpy's
"found in sys.modules" warning for ``python -m celestia_devtools.ci.job_timeouts``.
"""
