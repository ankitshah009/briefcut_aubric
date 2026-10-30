"""vedit — local, agent-native video editor."""

from vedit.edit import Edit
from vedit.ir import Brief, Canvas, load_brief
from vedit.media import probe
from vedit.compile import compile_brief
from vedit.agent.loop import run_session

__all__ = ["Edit", "Brief", "Canvas", "load_brief", "compile_brief", "probe", "run_session"]
__version__ = "1.2.0"
