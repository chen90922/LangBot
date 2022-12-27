import logging

import openai

import config

import pkg.openai.keymgr
import pkg.openai.pricing as pricing

inst = None


# 为其他模块提供与OpenAI交互的接口
class OpenAIInteract:
    api_params = {}

    key_mgr = None

    default_image_api_params = {
        "size": "256x256",
    }

    def __init__(self, api_key: str):
        # self.api_key = api_key

        self.key_mgr = pkg.openai.keymgr.KeysManager(api_key)

        openai.api_key = self.key_mgr.get_using_key()

        global inst
        inst = self

    # 请求OpenAI Completion
    def request_completion(self, prompt, stop):
        response = openai.Completion.create(
            prompt=prompt,
            stop=stop,
            timeout=config.process_message_timeout,
            **config.completion_api_params
        )

        switched = self.key_mgr.report_fee(pricing.language_base_price(config.completion_api_params['model'],
                                                                       prompt + response['choices'][0]['text']))

        if switched:
            openai.api_key = self.key_mgr.get_using_key()

        return response

    def request_image(self, prompt):

        params = config.image_api_params if hasattr(config, "image_api_params") else self.default_image_api_params

        response = openai.Image.create(
            prompt=prompt,
            n=1,
            **params
        )

        switched = self.key_mgr.report_fee(pricing.image_price(params['size']))

        if switched:
            openai.api_key = self.key_mgr.get_using_key()

        return response


def get_inst() -> OpenAIInteract:
    global inst
    return inst
