from __future__ import annotations

import typing
import json
import uuid

from .. import runner
from ...core import entities as core_entities
from .. import entities as llm_entities
from ...utils import image

from libs.dify_service_api.v1 import client, errors


@runner.runner_class("dify-service-api")
class DifyServiceAPIRunner(runner.RequestRunner):
    """Dify Service API 对话请求器"""

    dify_client: client.AsyncDifyServiceClient

    async def initialize(self):
        """初始化"""
        valid_app_types = ["chat", "agent", "workflow"]
        if (
            self.ap.provider_cfg.data["dify-service-api"]["app-type"]
            not in valid_app_types
        ):
            raise errors.DifyAPIError(
                f"不支持的 Dify 应用类型: {self.ap.provider_cfg.data['dify-service-api']['app-type']}"
            )

        api_key = self.ap.provider_cfg.data["dify-service-api"][
            self.ap.provider_cfg.data["dify-service-api"]["app-type"]
        ]["api-key"]

        self.dify_client = client.AsyncDifyServiceClient(
            api_key=api_key,
            base_url=self.ap.provider_cfg.data["dify-service-api"]["base-url"],
        )

    async def _preprocess_user_message(
        self, query: core_entities.Query
    ) -> tuple[str, list[str]]:
        """预处理用户消息，提取纯文本，并将图片上传到 Dify 服务

        Returns:
            tuple[str, list[str]]: 纯文本和图片的 Dify 服务图片 ID
        """
        plain_text = ""
        image_ids = []
        if isinstance(query.user_message.content, list):
            for ce in query.user_message.content:
                if ce.type == "text":
                    plain_text += ce.text
                elif ce.type == "image_url":
                    file_bytes, image_format = await image.get_qq_image_bytes(
                        ce.image_url.url
                    )
                    file = ("img.png", file_bytes, f"image/{image_format}")
                    file_upload_resp = await self.dify_client.upload_file(
                        file,
                        f"{query.session.launcher_type.value}_{query.session.launcher_id}",
                    )
                    image_id = file_upload_resp["id"]
                    image_ids.append(image_id)
        elif isinstance(query.user_message.content, str):
            plain_text = query.user_message.content

        return plain_text, image_ids

    async def _chat_messages(
        self, query: core_entities.Query
    ) -> typing.AsyncGenerator[llm_entities.Message, None]:
        """调用聊天助手"""
        cov_id = query.session.using_conversation.uuid or ""

        plain_text, image_ids = await self._preprocess_user_message(query)

        files = [
            {
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": image_id,
            }
            for image_id in image_ids
        ]

        mode = "basic"  # 标记是基础编排还是工作流编排

        basic_mode_pending_chunk = ''

        async for chunk in self.dify_client.chat_messages(
            inputs={},
            query=plain_text,
            user=f"{query.session.launcher_type.value}_{query.session.launcher_id}",
            conversation_id=cov_id,
            files=files,
            timeout=self.ap.provider_cfg.data["dify-service-api"]["chat"]["timeout"],
        ):
            self.ap.logger.debug("dify-chat-chunk: ", chunk)

            if chunk['event'] == 'workflow_started':
                mode = "workflow"

            if mode == "workflow":
                if chunk['event'] == 'node_finished':
                    if chunk['data']['node_type'] == 'answer':
                        yield llm_entities.Message(
                            role="assistant",
                            content=chunk['data']['outputs']['answer'],
                        )
            elif mode == "basic":
                if chunk['event'] == 'message':
                    basic_mode_pending_chunk += chunk['answer']
                elif chunk['event'] == 'message_end':
                    yield llm_entities.Message(
                        role="assistant",
                        content=basic_mode_pending_chunk,
                    )
                    basic_mode_pending_chunk = ''

        query.session.using_conversation.uuid = chunk["conversation_id"]

    async def _agent_chat_messages(
        self, query: core_entities.Query
    ) -> typing.AsyncGenerator[llm_entities.Message, None]:
        """调用聊天助手"""
        cov_id = query.session.using_conversation.uuid or ""

        plain_text, image_ids = await self._preprocess_user_message(query)

        files = [
            {
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": image_id,
            }
            for image_id in image_ids
        ]

        ignored_events = ["agent_message"]

        async for chunk in self.dify_client.chat_messages(
            inputs={},
            query=plain_text,
            user=f"{query.session.launcher_type.value}_{query.session.launcher_id}",
            response_mode="streaming",
            conversation_id=cov_id,
            files=files,
            timeout=self.ap.provider_cfg.data["dify-service-api"]["chat"]["timeout"],
        ):
            self.ap.logger.debug("dify-agent-chunk: ", chunk)
            if chunk["event"] in ignored_events:
                continue
            if chunk["event"] == "agent_thought":

                if chunk['tool'] != '' and chunk['observation'] != '':  # 工具调用结果，跳过
                    continue

                if chunk['thought'].strip() != '':  # 文字回复内容
                    msg = llm_entities.Message(
                        role="assistant",
                        content=chunk["thought"],
                    )
                    yield msg

                if chunk['tool']:
                    msg = llm_entities.Message(
                        role="assistant",
                        tool_calls=[
                            llm_entities.ToolCall(
                                id=chunk['id'],
                                type="function",
                                function=llm_entities.FunctionCall(
                                    name=chunk["tool"],
                                    arguments=json.dumps({}),
                                ),
                            )
                        ],
                    )
                    yield msg

        query.session.using_conversation.uuid = chunk["conversation_id"]

    async def _workflow_messages(
        self, query: core_entities.Query
    ) -> typing.AsyncGenerator[llm_entities.Message, None]:
        """调用工作流"""

        if not query.session.using_conversation.uuid:
            query.session.using_conversation.uuid = str(uuid.uuid4())

        cov_id = query.session.using_conversation.uuid

        plain_text, image_ids = await self._preprocess_user_message(query)

        files = [
            {
                "type": "image",
                "transfer_method": "local_file",
                "upload_file_id": image_id,
            }
            for image_id in image_ids
        ]

        ignored_events = ["text_chunk", "workflow_started"]

        async for chunk in self.dify_client.workflow_run(
            inputs={
                "langbot_user_message_text": plain_text,
                "langbot_session_id": f"{query.session.launcher_type.value}_{query.session.launcher_id}",
                "langbot_conversation_id": cov_id,
            },
            user=f"{query.session.launcher_type.value}_{query.session.launcher_id}",
            files=files,
            timeout=self.ap.provider_cfg.data["dify-service-api"]["workflow"]["timeout"],
        ):
            self.ap.logger.debug("dify-workflow-chunk: ", chunk)
            if chunk["event"] in ignored_events:
                continue

            if chunk["event"] == "node_started":

                if (
                    chunk["data"]["node_type"] == "start"
                    or chunk["data"]["node_type"] == "end"
                ):
                    continue

                msg = llm_entities.Message(
                    role="assistant",
                    content=None,
                    tool_calls=[
                        llm_entities.ToolCall(
                            id=chunk["data"]["node_id"],
                            type="function",
                            function=llm_entities.FunctionCall(
                                name=chunk["data"]["title"],
                                arguments=json.dumps({}),
                            ),
                        )
                    ],
                )

                yield msg

            elif chunk["event"] == "workflow_finished":
                if chunk['data']['error']:
                    raise errors.DifyAPIError(chunk['data']['error'])

                msg = llm_entities.Message(
                    role="assistant",
                    content=chunk["data"]["outputs"][
                        self.ap.provider_cfg.data["dify-service-api"]["workflow"][
                            "output-key"
                        ]
                    ],
                )

                yield msg

    async def run(
        self, query: core_entities.Query
    ) -> typing.AsyncGenerator[llm_entities.Message, None]:
        """运行请求"""
        if self.ap.provider_cfg.data["dify-service-api"]["app-type"] == "chat":
            async for msg in self._chat_messages(query):
                yield msg
        elif self.ap.provider_cfg.data["dify-service-api"]["app-type"] == "agent":
            async for msg in self._agent_chat_messages(query):
                yield msg
        elif self.ap.provider_cfg.data["dify-service-api"]["app-type"] == "workflow":
            async for msg in self._workflow_messages(query):
                yield msg
        else:
            raise errors.DifyAPIError(
                f"不支持的 Dify 应用类型: {self.ap.provider_cfg.data['dify-service-api']['app-type']}"
            )
