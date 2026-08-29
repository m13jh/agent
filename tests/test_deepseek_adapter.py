"""DeepSeek Adapter 的 dotenv 配置读取测试。"""

from pathlib import Path

from python_agent.llm.deepseek_adapter import DeepSeekAdapter


def test_deepseek_adapter_reads_connection_settings_from_env_file(
    tmp_path: Path, monkeypatch
) -> None:
    """验证 API Key、base URL 和 timeout 都来自显式指定的 .env 文件。"""

    # 清掉外部环境，确保这个测试验证的确实是 dotenv 文件，而不是当前 Shell 的同名变量。
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BASE_URL", raising=False)
    monkeypatch.delenv("DEEPSEEK_TIMEOUT_SECONDS", raising=False)
    env_file = tmp_path / ".env"
    env_file.write_text(
        "DEEPSEEK_API_KEY=file-key\n"
        "DEEPSEEK_BASE_URL=https://example.invalid/v1\n"
        "DEEPSEEK_TIMEOUT_SECONDS=15\n",
        encoding="utf-8",
    )

    adapter = DeepSeekAdapter(env_file=env_file)

    assert adapter.api_key == "file-key"
    assert adapter.base_url == "https://example.invalid/v1"
    assert adapter.timeout_seconds == 15.0
    assert adapter.env_file == env_file.resolve()
