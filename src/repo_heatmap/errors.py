"""The one exception type for problems the user can fix."""


class HeatmapError(Exception):
    """Bad input or an unusable repository. The CLI prints the message and exits 1."""
