# MCPClient,单个MCP服务的客户端封装,支持 stdio子进程和streamable HTTP两种连接方式;负责建立会话,
# 初始化MCP,拉取工具列表,调用远端工具,并且用AsyncExitStack统一管理所有资源的释放
from __future__ import annotations

import logging
import os
# 异步资源栈,可以批量管理一堆async上下文管理器,一次性清理,非常适合MCP
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from mewcode.config import MCPServerConfig, build_child_env, resolve_env_vars

logger = logging.getLogger(__name__)


class MCPClient:
    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.name = config.name
        self._session: ClientSession | None = None
        self._stack: AsyncExitStack | None = None
        self._alive = False


    @property
    def is_alive(self) -> bool:
        return self._alive


    # 建立MCP连接
    async def connect(self) -> None:
        if self._alive:
            return

        self._stack = AsyncExitStack()
        await self._stack.__aenter__()

        try:
            if self.config.is_stdio:
                # stdio子进程方式
                read, write = await self._connect_stdio()
            else:
                # http方式
                read, write = await self._connect_http()

            session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await session.initialize()
            self._session = session
            self._alive = True
            logger.info("MCP server '%s' connected", self.name)
        except Exception:
            await self._cleanup_stack()
            raise

    # 启动本地子进程MCP服务
    async def _connect_stdio(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.command is not None

        params = StdioServerParameters(
            command=self.config.command,
            args=self.config.args,
            env=build_child_env(self.config.env),
        )
        devnull = open(os.devnull, "w")
        self._stack.callback(devnull.close)
        read, write = await self._stack.enter_async_context(
            # 启动子进程
            stdio_client(params, errlog=devnull)
        )
        return read, write

    # 私有方法，http模式MCP
    async def _connect_http(self) -> tuple[Any, Any]:
        assert self._stack is not None
        assert self.config.url is not None

        resolved_headers = {
            k: resolve_env_vars(v) for k, v in self.config.headers.items()
        }
        http_client = httpx.AsyncClient(
            headers=resolved_headers,
            follow_redirects=True,
        )
        await self._stack.enter_async_context(http_client)

        result = await self._stack.enter_async_context(
            streamable_http_client(self.config.url, http_client=http_client)
        )
        read, write = result[0], result[1]
        return read, write

    # 调用MCP协议的list_tools方法,返回工具列表
    async def list_tools(self) -> list[types.Tool]:
        assert self._session is not None
        result = await self._session.list_tools()
        return list(result.tools)

    # 远端调用MCP工具，传参数并返回结果
    async def call_tool(
        self, name: str, arguments: dict[str, Any]
    ) -> types.CallToolResult:
        assert self._session is not None
        return await self._session.call_tool(name, arguments)

    # 对外关闭入口，标记断开，触发栈清理
    async def close(self) -> None:
        self._alive = False
        self._session = None
        await self._cleanup_stack()

    async def _cleanup_stack(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.__aexit__(None, None, None)
            except RuntimeError as e:
                if "cancel scope" in str(e):
                    logger.debug("Cancel scope cleanup (expected during shutdown): %s", e)
                else:
                    raise
            except Exception:
                logger.debug("Error closing stack for '%s'", self.name, exc_info=True)
            self._stack = None
