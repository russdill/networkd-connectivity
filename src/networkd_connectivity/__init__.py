
# networkd-connectivity - per-interface connectivity probe.
# This sub-package only exposes a version string for "pip show".

from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version(__name__)
except PackageNotFoundError:        # running from a checkout
    __version__ = "0.0.0+dev"
