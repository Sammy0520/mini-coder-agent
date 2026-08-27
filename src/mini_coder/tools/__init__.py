from .command import RunCommandTool
from .filesystem import EditFileTool, ListFilesTool, ReadFileTool, WriteFileTool
from .registry import ToolRegistry
from .search import SearchTextTool

__all__ = [
    "EditFileTool",
    "ListFilesTool",
    "ReadFileTool",
    "RunCommandTool",
    "SearchTextTool",
    "ToolRegistry",
    "WriteFileTool",
]


def create_default_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(ListFilesTool())
    registry.register(ReadFileTool())
    registry.register(SearchTextTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(RunCommandTool())
    return registry

