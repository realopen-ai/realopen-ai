"""Tool exports

All tools are imported here so they register themselves in the global
ToolRegistry on startup. The module system then conditionally activates
or deactivates tools based on which modules are enabled.
"""

from app.agent.tools.web_search import WebSearchTool
from app.agent.tools.vision import VisionTool
from app.agent.tools.code_exec import CodeExecTool
from app.agent.tools.web_fetch import WebFetchTool
from app.agent.tools.image_gen import ImageGenTool
from app.agent.tools.rag_search import RagSearchTool
from app.agent.tools.manage_memory import ManageMemoryTool
from app.agent.tools.search_past_conversations import SearchPastConversationsTool
from app.agent.tools.report_gen import ReportGenTool
from app.agent.tools.pptx_gen import PptxGenTool
from app.agent.tools.excel_gen import ExcelGenTool
from app.agent.tools.delegate_coder import DelegateCoderTool

__all__ = [
    "WebSearchTool",
    "VisionTool",
    "CodeExecTool",
    "WebFetchTool",
    "ImageGenTool",
    "RagSearchTool",
    "ManageMemoryTool",
    "SearchPastConversationsTool",
    "ReportGenTool",
    "PptxGenTool",
    "ExcelGenTool",
    "DelegateCoderTool",
]
