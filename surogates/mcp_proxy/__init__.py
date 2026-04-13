"""MCP Proxy — standalone credential-injecting proxy service.

Deployed as a separate K8s Deployment, this service sits between
untrusted sandbox pods and external MCP servers.  It resolves
credentials from the vault, injects them into MCP server connections,
and ensures secrets never reach the sandbox environment.
"""
