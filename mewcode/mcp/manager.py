# MCPManager 是MCP服务器的管理器,维护多个MCP服务的配置和连接,负责批量注册MCP工具到本地工具注册表,支持懒重连,程序关闭时同意断开所有mcp连接

from __future__ import annotations

import logging

from mewcode.config import MCPServerConfig
from mewcode.mcp.client import MCPClient
from mewcode.mcp.tool_wrapper import MCPToolWrapper
from mewcode.tools import ToolRegistry

logger = logging.getLogger(__name__)


class MCPManager:


    def __init__(self) -> None:
        self._configs: dict[str, MCPServerConfig] = {}
        self._clients: dict[str, MCPClient] = {}

    # 存配置
    def load_configs(self, configs: list[MCPServerConfig]) -> None:
        for cfg in configs:
            self._configs[cfg.name] = cfg

    # 异步，一次性连接所有MCP服务，拉取工具列表，注册到全局工具注册表
    # 遍历每一个MCP服务配置
    # 创建MCPClient实例，连接MCP服务，获取工具列表
    async def register_all_tools(self, registry: ToolRegistry) -> list[str]:
        errors: list[str] = []
        for name, config in self._configs.items():
            try:
                # 创建单个MCP客户端
                client = MCPClient(config)
                # 建立和这个MCP服务的连接（启动子进程 / 发起http连接）
                await client.connect()
                # 连接成功，缓存这个client
                self._clients[name] = client

                # 调用MCP协议：向远端查询该服务暴露的所有接口
                tools = await client.list_tools()
                # 遍历远端返回的每个工具定义
                for tool_def in tools:
                    # 包装：MCPToolWrapper，把远端MCP工具封装成Mewcode内部工具对象
                    wrapper = MCPToolWrapper(name, tool_def, client)
                    # 注册进全局工具注册表，模型之后就能看到并调用这个工具了
                    registry.register(wrapper)
                    logger.info("Registered MCP tool: %s", wrapper.name)

            except Exception as e:
                # 某个MCP服务连接失败，捕获异常，不中断循环，继续下一个服务
                msg = f"MCP server '{name}': {e}"
                logger.warning(msg)
                errors.append(msg)

        return errors


    async def get_client(self, name: str) -> MCPClient | None:
        # 从缓存查找已经连接好的客户端
        client = self._clients.get(name)
        if client is None:
            config = self._configs.get(name)
            # 缓存里没有这个client：懒加载
            if config is None:
                # 找不到这个名字对应的配置，直接返回None
                return None
            # 创建新client+连接
            client = MCPClient(config)
            await client.connect()
            self._clients[name] = client
            return client

        # client存在，但是连接已经挂掉（is_alive=False）
        if not client.is_alive:
            logger.info("Reconnecting MCP server '%s'", name)
            await client.close()
            # 新建客户端，重新连接，替代缓存里的旧client
            client = MCPClient(self._configs[name])
            await client.connect()
            self._clients[name] = client

        return client


    async def shutdown(self) -> None:
        for name, client in self._clients.items():
            try:
                await client.close()
                logger.info("MCP server '%s' closed", name)
            except Exception:
                # 关闭的时候发生异常，只打debug日志，不向上抛，保证其他连接继续关闭
                logger.debug("Error closing MCP server '%s'", name, exc_info=True)
        # 清空client缓存
        self._clients.clear()
