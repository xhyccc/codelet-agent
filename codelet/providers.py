"""OpenAI-compatible LLM provider presets.

Each preset declares the default base URL and the environment variable
conventionally used for the API key. All providers expose an OpenAI-compatible
``/v1/chat/completions`` endpoint, so :class:`OpenAIModelClient` is reused for
every entry.
"""


LLM_PROVIDER_PRESETS = {
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "description": "OpenAI",
    },
    "kimi": {
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-8k",
        "description": "Moonshot AI (Kimi)",
    },
    "moonshot": {
        "base_url": "https://api.moonshot.cn/v1",
        "env_key": "MOONSHOT_API_KEY",
        "default_model": "moonshot-v1-8k",
        "description": "Moonshot AI (Kimi)",
    },
    "glm": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "default_model": "glm-4-flash",
        "description": "Zhipu AI (GLM)",
    },
    "zhipu": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZHIPU_API_KEY",
        "default_model": "glm-4-flash",
        "description": "Zhipu AI (GLM)",
    },
    "siliconflow": {
        "base_url": "https://api.siliconflow.cn/v1",
        "env_key": "SILICONFLOW_API_KEY",
        "default_model": "Qwen/Qwen2.5-7B-Instruct",
        "description": "SiliconFlow",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "env_key": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
        "description": "DeepSeek",
    },
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "openai/gpt-4o-mini",
        "description": "OpenRouter",
    },
    "together": {
        "base_url": "https://api.together.xyz/v1",
        "env_key": "TOGETHER_API_KEY",
        "default_model": "meta-llama/Meta-Llama-3.1-8B-Instruct-Turbo",
        "description": "Together AI",
    },
    "dashscope": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "env_key": "DASHSCOPE_API_KEY",
        "default_model": "qwen-plus",
        "description": "Alibaba DashScope (Qwen)",
    },
    "custom": {
        "base_url": None,
        "env_key": "CUSTOM_LLM_API_KEY",
        "default_model": None,
        "description": "Custom OpenAI-compatible endpoint",
    },
}


def resolve_provider_preset(name):
    """Look up an LLM provider preset by name (case-insensitive)."""
    if not name:
        return None
    return LLM_PROVIDER_PRESETS.get(name.lower())
