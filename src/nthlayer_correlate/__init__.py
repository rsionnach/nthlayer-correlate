"""nthlayer-correlate (DEPRECATED) — superseded by nthlayer-workers (correlate modules).

This package is deprecated as of v1.0.0 (2026-04-28). Functionality moved to
nthlayer-workers as part of the v1.5 tiered architecture consolidation.

Replacement: pip install nthlayer-workers

The correlate functionality is now implemented as three worker modules in
nthlayer-workers:

  - CorrelateSessionModule (session-window event correlation)
  - CorrelateTopologyModule (topology drift detection)
  - CorrelateContractModule (promised vs observed contract divergence)

Migration: https://github.com/rsionnach/nthlayer-correlate
"""

import warnings as _warnings

_warnings.warn(
    "nthlayer-correlate is deprecated. Functionality moved to nthlayer-workers "
    "as of v1.5 (correlate modules: CorrelateSessionModule, "
    "CorrelateTopologyModule, CorrelateContractModule). "
    "Install: pip install nthlayer-workers. "
    "Migration: https://github.com/rsionnach/nthlayer-correlate",
    DeprecationWarning,
    stacklevel=2,
)
del _warnings
