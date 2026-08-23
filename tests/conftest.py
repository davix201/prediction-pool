import sys

if sys.platform == "win32":
    # gltest's direct runner injects the transaction message by dup2-ing a
    # temp file onto fd 0 and then unlinking it. On Windows the file remains
    # locked through fd 0, so os.unlink raises PermissionError AFTER the
    # injection itself has already succeeded. Wrap the injector so this
    # benign cleanup failure does not abort contract deployment.
    from gltest.direct import loader as _loader

    _original_inject = _loader._inject_message_to_fd0

    def _windows_safe_inject(vm) -> None:
        try:
            _original_inject(vm)
        except PermissionError:
            pass

    _loader._inject_message_to_fd0 = _windows_safe_inject
