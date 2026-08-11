"""Support helpers."""
from folder_watch.support.copy_io import atomic_copy
from folder_watch.support.launchd import (
    install_service,
    service_status,
    uninstall_service,
)
from folder_watch.support.log import (
    emit,
    emit_debug,
    is_verbose,
    set_json_mode,
    set_verbose,
    setup_logging,
)
from folder_watch.support.partials import is_partial_filename
from folder_watch.support.pidfile import (
    clear_pidfile,
    collect_watch_status,
    write_pidfile,
)
from folder_watch.support.state import DoneStore, file_identity

__all__ = [
    "DoneStore",
    "atomic_copy",
    "clear_pidfile",
    "collect_watch_status",
    "emit",
    "emit_debug",
    "file_identity",
    "install_service",
    "is_partial_filename",
    "is_verbose",
    "service_status",
    "set_json_mode",
    "set_verbose",
    "setup_logging",
    "uninstall_service",
    "write_pidfile",
]
