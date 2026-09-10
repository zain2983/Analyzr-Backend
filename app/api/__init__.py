# Intentionally empty.
#
# This module previously defined a `/run-script` router that wrote the dataset
# to a temp file and shelled out to `subprocess.run(['python', <hardcoded
# path>, csv_path])`. It was never mounted in app.main, but leaving a
# subprocess-spawning handler in an importable package is a loaded gun: one
# stray `include_router(api.router)` turns it into remote code execution, and
# the hardcoded path pointed at a developer's home directory. Removed rather
# than left dormant.
