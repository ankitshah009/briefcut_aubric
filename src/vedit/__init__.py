"""vedit — local, agent-native video editor.

Briefcut by Aubric, Inc.: a local video editor that Meta's models plan and FFmpeg cuts.
Copyright (C) 2026 Aubric, Inc.

This program is free software: you can redistribute it and/or modify it under
the terms of the GNU Affero General Public License as published by the Free
Software Foundation, either version 3 of the License, or (at your option) any
later version.

This program is distributed in the hope that it will be useful, but WITHOUT ANY
WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License along
with this program, in the LICENSE file. If not, see <https://www.gnu.org/licenses/>.
"""

from vedit.edit import Edit
from vedit.ir import Brief, Canvas, load_brief
from vedit.media import probe
from vedit.compile import compile_brief
from vedit.agent.loop import run_session

__all__ = ["Edit", "Brief", "Canvas", "load_brief", "compile_brief", "probe", "run_session"]
__version__ = "1.2.0"
__license__ = "AGPL-3.0-or-later"
__copyright__ = "Copyright (C) 2026 Aubric, Inc."
