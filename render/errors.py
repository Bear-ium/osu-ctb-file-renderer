class RenderError(Exception):
    """An error whose message is safe to show to the person who sent the replay."""


class ReplayError(RenderError):
    pass


class BeatmapError(RenderError):
    pass
