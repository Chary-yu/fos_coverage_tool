"""FOS Coverage application package."""

# Install the legacy-Git adapter before application submodules start Git
# subprocesses.  It is a command-transport compatibility shim only.
from app.git_runtime_compat import install as _install_git_runtime_compat

_install_git_runtime_compat()
del _install_git_runtime_compat
