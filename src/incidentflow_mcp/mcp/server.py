"""MCP server bootstrap."""

import logging

from mcp.server.fastmcp import FastMCP

from incidentflow_mcp.config import get_settings
from incidentflow_mcp.integrations import resolve_tool_integration_context
from incidentflow_mcp.mcp.access import ToolAccessResolver
from incidentflow_mcp.mcp.compatibility.fastmcp_contracts import harden_fastmcp_tool_contracts
from incidentflow_mcp.mcp.context import ToolRegistrationContext
from incidentflow_mcp.mcp.registration import slack as _slack_registration
from incidentflow_mcp.mcp.registration.argocd import register_argocd_tools
from incidentflow_mcp.mcp.registration.async_jobs import register_async_tools
from incidentflow_mcp.mcp.registration.grafana import register_grafana_tools
from incidentflow_mcp.mcp.registration.knowledge import register_knowledge_tools
from incidentflow_mcp.mcp.registration.kubernetes import register_kubernetes_tools
from incidentflow_mcp.mcp.registration.meta import register_meta_tools, registered_tool_metric_rows
from incidentflow_mcp.mcp.request_context import MCPRequestContext
from incidentflow_mcp.mcp.resources import register_resources
from incidentflow_mcp.mcp.services import slack_access as _slack_access_service
from incidentflow_mcp.mcp.services.memory_context import MemoryContextService
from incidentflow_mcp.mcp.workspace import WorkspaceResolver
from incidentflow_mcp.observability.metrics import publish_registered_tools
from incidentflow_mcp.tools.registry import get_tool_specs

logger = logging.getLogger(__name__)


def create_mcp_server() -> FastMCP:
    """
    Instantiate and configure the FastMCP server with all registered tools.

    Returns a FastMCP instance whose `streamable_http_app()` can be mounted
    into a FastAPI/Starlette application.
    """
    settings = get_settings()

    mcp = FastMCP(
        name=settings.mcp_server_name,
        host="0.0.0.0",
        stateless_http=True,
        streamable_http_path="/mcp",
    )

    _specs = {s.name: s for s in get_tool_specs()}
    ctx = ToolRegistrationContext(mcp=mcp, settings=settings, specs=_specs)
    request_context = MCPRequestContext(settings)
    tool_access = ToolAccessResolver(
        settings=settings,
        request_context=request_context,
        specs=_specs,
        integration_context_resolver=resolve_tool_integration_context,
    )
    slack_access = _slack_access_service.SlackAccessResolver(settings)
    workspace_resolver = WorkspaceResolver(
        default_workspace_id=settings.mcp_default_workspace_id,
        request_context=request_context,
    )

    register_meta_tools(ctx)
    register_knowledge_tools(ctx, current_token_workspace_id=workspace_resolver.token_workspace_id)

    memory_context = MemoryContextService(
        settings,
        resolve_workspace_id=workspace_resolver.resolve,
        current_token_workspace_id=workspace_resolver.token_workspace_id,
    )

    register_async_tools(
        ctx,
        memory=memory_context,
        current_token_workspace_id=workspace_resolver.token_workspace_id,
    )

    _slack_registration.register_slack_tools(
        ctx,
        memory=memory_context,
        resolve_tool_guard=tool_access.resolve,
        current_token_workspace_id=workspace_resolver.token_workspace_id,
        resolve_slack_access=slack_access.resolve,
        workspace_context_required_error=_slack_access_service.workspace_context_required_error,
        platform_slack_error_json=_slack_access_service.platform_slack_error_json,
    )

    register_kubernetes_tools(
        ctx,
        memory=memory_context,
        resolve_tool_guard=tool_access.resolve,
        current_bearer_token=request_context.bearer_token,
    )

    register_argocd_tools(
        ctx,
        resolve_workspace_id=workspace_resolver.resolve_from_token,
        current_token_workspace_id=workspace_resolver.token_workspace_id,
    )
    register_grafana_tools(
        ctx,
        resolve_workspace_id=workspace_resolver.resolve_from_token,
        current_token_workspace_id=workspace_resolver.token_workspace_id,
    )

    publish_registered_tools(registered_tool_metric_rows())
    harden_fastmcp_tool_contracts(mcp)
    register_resources(mcp)

    return mcp
